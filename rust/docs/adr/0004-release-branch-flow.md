# ADR 0004 — Release-branch flow

Status: accepted (RUST-00)

* Integration branch: `release/rust-rewrite-v1`, created once from `main` at
  `1e92ed42e258898572e8dc57b1ce2608435686d9`.
* Each phase `RUST-NN` has exactly one feature branch named in the plan
  (`feature/rust-NN-…`), created from the current release head after all of
  its dependency phases are merged.
* Every rewrite PR uses base `release/rust-rewrite-v1`. PRs from
  `feature/rust-*` into any other base, or from the release branch into
  `main`/`master`, fail the `Rewrite PR base guard` job in
  `.github/workflows/rust.yml`.
* Phases merge sequentially; the next phase never starts from an unmerged
  feature branch.
* After RUST-16 the release branch is tagged and validated. Promotion to
  `main` is a separate human decision outside the plan.
