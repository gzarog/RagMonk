import os
from typing import List


class Animal:
    pass


class Dog(Animal):
    """A dog."""

    species: str = "canine"

    def __init__(self, name):
        self.name = name

    def bark(self) -> str:
        return self.speak()

    def speak(self):
        return "woof"


@app.route("/dogs", methods=["GET"])
def list_dogs():
    d = Dog("rex")
    return d.bark()
