import { makeDog } from "./animals.js";

export class App {
  start() {
    return makeDog("rex").bark();
  }
}

export function main() {
  new App().start();
  console.log("done");
}
