namespace Shop.Payments
{
    public class PaymentGateway
    {
        [HttpPost("api/payments/charge")]
        public void Charge(int amount) { }

        public void Refund(int amount) { }
    }
}
