import fs from "fs";
import { readFile } from "node:fs";

class Animal {
  speak() { return "..."; }
}

class Dog extends Animal {
  name = "rex";

  constructor(name) {
    super();
    this.name = name;
  }

  bark() {
    return this.speak();
  }
}

function makeDog(name) {
  const d = new Dog(name);
  return d.bark();
}
