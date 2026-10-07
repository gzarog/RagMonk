use crate::animals::Dog;
use std::io::Write as W;

pub trait Greeter {
    fn greet(&self) -> String;
    fn hello(&self) -> String { self.greet() }
}

pub struct Bot { name: String }

impl Bot {
    pub fn new(name: &str) -> Self { Bot { name: name.to_string() } }
}

impl Greeter for Bot {
    fn greet(&self) -> String { format!("hi {}", self.name) }
}

pub enum Mode { A, B }

fn main() {
    let b = Bot::new("x");
    println!("{}", b.greet());
    Dog::new();
}
