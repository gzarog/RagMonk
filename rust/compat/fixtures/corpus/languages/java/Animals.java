package com.example.animals;

import java.util.List;
import java.util.ArrayList;

public interface Animal {
    String speak();
}

public class Dog implements Animal {
    private String name;

    public Dog(String name) {
        this.name = name;
    }

    public String speak() {
        return bark();
    }

    private String bark() {
        return name + " says woof";
    }
}

class Puppy extends Dog {
    public Puppy(String name) {
        super(name);
    }
}
