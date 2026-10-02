using System;
using System.Collections.Generic;

namespace Animals
{
    public interface IAnimal
    {
        string Speak();
    }

    public class Dog : IAnimal
    {
        public string Name { get; set; }

        public Dog(string name)
        {
            Name = name;
        }

        [Route("api/dogs/{id}")]
        [HttpGet]
        public string Speak()
        {
            return Bark();
        }

        private string Bark()
        {
            return Name + " says woof";
        }
    }

    public class Puppy : Dog
    {
        public Puppy(string name) : base(name) { }
    }
}
