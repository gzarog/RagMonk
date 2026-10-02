import React from "react";
import { Dog } from "../animals";

interface Props { name: string }

export class View extends React.Component<Props> implements Renderable {
  @Input() title: string;

  @HostListener("click")
  onClick(): void {
    const d = new Dog("rex");
    d.bark();
  }

  render() {
    return <div>{this.props.name}</div>;
  }
}

export function helper(x: number): number {
  return Math.max(x, 1);
}
