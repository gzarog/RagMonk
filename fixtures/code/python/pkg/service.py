"""Cross-file references into animals.py."""
import animals
from pkg import helpers as h
import os.path as osp


class Kennel(animals.Dog):
    capacity = 10

    def add(self, dog):
        dog.bark()
        helper()
        return animals.Dog("x")

    def add(self, dog, extra):
        return osp.join("a", "b")


@app.get("/kennels/<id>")
@login_required
def get_kennel(id):
    return Kennel().add(None)


@router.route("/kennels", methods=['POST'])
def create_kennel():
    return unknown_function()


def helper():
    return speak()
