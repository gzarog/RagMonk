export class OrderController {
  placeOrder(id: string): string { return id; }
  cancelOrder(id: string): string { return id; }
}

export function formatCurrency(value: number): string {
  return value.toFixed(2);
}
