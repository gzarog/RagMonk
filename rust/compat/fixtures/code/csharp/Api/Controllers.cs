using Microsoft.AspNetCore.Mvc;
using Animals;

namespace Api.Controllers
{
    [ApiController]
    public class DogsController : ControllerBase, IDisposable
    {
        private readonly int count = 0;

        [HttpPost("api/dogs")]
        public IActionResult Create(string name)
        {
            var d = new Dog(name);
            return Ok(d.Speak());
        }

        [HttpDelete]
        public void Delete(int id) { }

        [Route("api/dogs/all")]
        public void List() { }

        public void List(int page) { Helper.Run(page); }

        public void Dispose() { }
    }

    public struct Point { public int X; public int Y; }

    public enum Color { Red, Green }
}
