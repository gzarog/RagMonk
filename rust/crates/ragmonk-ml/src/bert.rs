//! A lean BERT encoder for CPU inference.
//!
//! Numerically it is the reference BERT forward pass (post-norm layers,
//! exact-erf GELU, `(1 - mask) * f32::MIN` attention bias), restructured
//! for Candle's CPU backend: activations stay 2-D `(tokens, hidden)` so
//! every projection is one contiguous GEMM with a pre-transposed weight,
//! Q/K/V are fused into a single projection, layer norm and softmax use
//! Candle's fused kernels, and GELU runs in parallel.

use candle_core::{CpuStorage, CustomOp1, DType, Layout, Result, Shape, Tensor};
use candle_nn::VarBuilder;
use rayon::prelude::*;

#[derive(Debug, Clone, serde::Deserialize)]
pub struct BertConfig {
    pub hidden_size: usize,
    pub num_attention_heads: usize,
    pub num_hidden_layers: usize,
    pub intermediate_size: usize,
    #[serde(default = "default_eps")]
    pub layer_norm_eps: f64,
    pub max_position_embeddings: usize,
    #[serde(default = "default_types")]
    pub type_vocab_size: usize,
    pub vocab_size: usize,
    #[serde(default = "default_act")]
    pub hidden_act: String,
}

fn default_eps() -> f64 {
    1e-12
}
fn default_types() -> usize {
    2
}
fn default_act() -> String {
    "gelu".into()
}

/// `y = x · W + b` with `W` stored transposed `(in, out)` and contiguous.
struct Dense {
    w: Tensor,
    b: Tensor,
}

impl Dense {
    fn load(vb: VarBuilder, input: usize, output: usize) -> Result<Self> {
        let w = vb.get((output, input), "weight")?.t()?.contiguous()?;
        let b = vb.get(output, "bias")?;
        Ok(Self { w, b })
    }

    fn fused(parts: &[Dense]) -> Result<Self> {
        let ws: Vec<&Tensor> = parts.iter().map(|d| &d.w).collect();
        let bs: Vec<&Tensor> = parts.iter().map(|d| &d.b).collect();
        Ok(Self {
            w: Tensor::cat(&ws, 1)?.contiguous()?,
            b: Tensor::cat(&bs, 0)?,
        })
    }

    fn forward(&self, x: &Tensor) -> Result<Tensor> {
        x.matmul(&self.w)?.broadcast_add(&self.b)
    }
}

struct Norm {
    w: Tensor,
    b: Tensor,
    eps: f32,
}

impl Norm {
    fn load(vb: VarBuilder, size: usize, eps: f64) -> Result<Self> {
        Ok(Self {
            w: vb.get(size, "weight")?,
            b: vb.get(size, "bias")?,
            eps: eps as f32,
        })
    }

    fn forward(&self, x: &Tensor) -> Result<Tensor> {
        candle_nn::ops::layer_norm(x, &self.w, &self.b, self.eps)
    }
}

struct Layer {
    qkv: Dense,
    attn_out: Dense,
    attn_norm: Norm,
    inter: Dense,
    out: Dense,
    out_norm: Norm,
}

pub struct Bert {
    word: Tensor,
    position: Tensor,
    token_type: Tensor,
    emb_norm: Norm,
    layers: Vec<Layer>,
    heads: usize,
    head_dim: usize,
    hidden: usize,
}

/// Exact-erf GELU, parallel over contiguous f32 input.
struct GeluErf;

impl CustomOp1 for GeluErf {
    fn name(&self) -> &'static str {
        "ragmonk-gelu-erf"
    }

    fn cpu_fwd(&self, storage: &CpuStorage, layout: &Layout) -> Result<(CpuStorage, Shape)> {
        let (start, end) = layout
            .contiguous_offsets()
            .ok_or_else(|| candle_core::Error::Msg("gelu input must be contiguous".into()))?;
        let src = &storage.as_slice::<f32>()?[start..end];
        let mut dst = vec![0f32; src.len()];
        dst.par_chunks_mut(4096)
            .zip(src.par_chunks(4096))
            .for_each(|(d, s)| {
                for (o, &v) in d.iter_mut().zip(s) {
                    *o = (candle_core::cpu::erf::erf_f32(v * std::f32::consts::FRAC_1_SQRT_2)
                        + 1.0)
                        * 0.5
                        * v;
                }
            });
        Ok((CpuStorage::F32(dst), layout.shape().clone()))
    }
}

impl Bert {
    pub fn load(vb: VarBuilder, cfg: &BertConfig) -> Result<Self> {
        if cfg.hidden_act != "gelu" {
            candle_core::bail!("unsupported hidden_act {}", cfg.hidden_act);
        }
        // sentence-transformers exports have no prefix; plain HF BERT uses
        // `bert.`.
        let vb = if vb.contains_tensor("embeddings.word_embeddings.weight") {
            vb
        } else {
            vb.pp("bert")
        };
        let h = cfg.hidden_size;
        let e = vb.pp("embeddings");
        let emb_norm = Norm::load(e.pp("LayerNorm"), h, cfg.layer_norm_eps)?;
        let mut layers = Vec::with_capacity(cfg.num_hidden_layers);
        for i in 0..cfg.num_hidden_layers {
            let l = vb.pp(format!("encoder.layer.{i}"));
            let a = l.pp("attention");
            let s = a.pp("self");
            let qkv = Dense::fused(&[
                Dense::load(s.pp("query"), h, h)?,
                Dense::load(s.pp("key"), h, h)?,
                Dense::load(s.pp("value"), h, h)?,
            ])?;
            layers.push(Layer {
                qkv,
                attn_out: Dense::load(a.pp("output.dense"), h, h)?,
                attn_norm: Norm::load(a.pp("output.LayerNorm"), h, cfg.layer_norm_eps)?,
                inter: Dense::load(l.pp("intermediate.dense"), h, cfg.intermediate_size)?,
                out: Dense::load(l.pp("output.dense"), cfg.intermediate_size, h)?,
                out_norm: Norm::load(l.pp("output.LayerNorm"), h, cfg.layer_norm_eps)?,
            });
        }
        Ok(Self {
            word: e.get((cfg.vocab_size, h), "word_embeddings.weight")?,
            position: e.get(
                (cfg.max_position_embeddings, h),
                "position_embeddings.weight",
            )?,
            token_type: e.get((cfg.type_vocab_size, h), "token_type_embeddings.weight")?,
            emb_norm,
            layers,
            heads: cfg.num_attention_heads,
            head_dim: h / cfg.num_attention_heads,
            hidden: h,
        })
    }

    /// `ids`/`mask`: `(batch, seq)` u32. Returns `(batch, seq, hidden)`.
    pub fn forward(&self, ids: &Tensor, mask: &Tensor) -> Result<Tensor> {
        let (b, s) = ids.dims2()?;
        let n = b * s;
        let device = ids.device();
        let flat = ids.flatten_all()?;
        let positions = Tensor::arange(0u32, s as u32, device)?;
        let pos = self.position.index_select(&positions, 0)?; // (s, h)
        let tok = self.token_type.get(0)?; // all-zero token types
        let x = self
            .word
            .index_select(&flat, 0)?
            .reshape((b, s, self.hidden))?
            .broadcast_add(&pos)?
            .broadcast_add(&tok)?
            .reshape((n, self.hidden))?;
        let mut x = self.emb_norm.forward(&x)?;
        // (b, 1, 1, s) additive bias: 0 for tokens, f32::MIN for padding.
        let bias = ((mask.to_dtype(DType::F32)?.ones_like()? - mask.to_dtype(DType::F32)?)?
            * f64::from(f32::MIN))?
        .reshape((b, 1, 1, s))?;
        let scale = 1.0 / (self.head_dim as f64).sqrt();
        for layer in &self.layers {
            let qkv = layer.qkv.forward(&x)?; // (n, 3h)
            let split = |i: usize| -> Result<Tensor> {
                qkv.narrow(1, i * self.hidden, self.hidden)?
                    .reshape((b, s, self.heads, self.head_dim))?
                    .transpose(1, 2)?
                    .contiguous()
            };
            let (q, k, v) = (split(0)?, split(1)?, split(2)?);
            let scores = (q.matmul(&k.t()?)? * scale)?.broadcast_add(&bias)?;
            let probs = candle_nn::ops::softmax_last_dim(&scores)?;
            let ctx = probs
                .matmul(&v)? // (b, heads, s, d)
                .transpose(1, 2)?
                .contiguous()?
                .reshape((n, self.hidden))?;
            let attn = layer
                .attn_norm
                .forward(&(layer.attn_out.forward(&ctx)? + &x)?)?;
            let inter = layer.inter.forward(&attn)?.apply_op1_no_bwd(&GeluErf)?;
            x = layer
                .out_norm
                .forward(&(layer.out.forward(&inter)? + &attn)?)?;
        }
        x.reshape((b, s, self.hidden))
    }
}
