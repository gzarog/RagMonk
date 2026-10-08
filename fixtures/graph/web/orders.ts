import { normalizeTotal } from "./money";

export class OrderController {
  placeOrder(total: number): number {
    return normalizeTotal(total);
  }

  cancelOrder(id: string): void {
    this.placeOrder(0);
    trackEvent(id);
  }
}
