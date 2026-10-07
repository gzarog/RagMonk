use std::fmt;
use std::collections::HashMap as Map;

pub trait Animal {
    fn speak(&self) -> String;
}

pub struct Dog {
    pub name: String,
}

impl Animal for Dog {
    fn speak(&self) -> String {
        self.bark()
    }
}

impl Dog {
    fn bark(&self) -> String {
        format!("{} says woof", self.name)
    }
}

fn make_dog(name: String) -> Dog {
    Dog { name }
}
