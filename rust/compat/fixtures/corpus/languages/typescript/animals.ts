import { Component } from "./base";
import type { Foo } from "./foo";

export interface Shape {
  area(): number;
}

export class Circle extends Component implements Shape {
  radius: number;
  private label: string = "c";

  constructor(radius: number) {
    super();
    this.radius = radius;
  }

  area(): number {
    return this.compute();
  }

  private compute(): number {
    return 3.14 * this.radius * this.radius;
  }
}

enum Color {
  Red,
  Green,
}
