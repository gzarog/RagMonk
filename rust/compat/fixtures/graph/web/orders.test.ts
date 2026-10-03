import { OrderController } from "./orders";

describe("orders", () => {
  it("places", () => {
    new OrderController().placeOrder(1);
  });
});
