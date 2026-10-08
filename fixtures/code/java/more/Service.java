package com.example.more;

import com.example.animals.Dog;

public class Service extends Base implements Runnable, Closeable {
    private int count = 0;

    @Override
    @Deprecated
    public void run() {
        Dog d = new Dog("x");
        d.speak();
        helper();
    }

    public void close() {}

    private void helper() {}
}

enum Level { LOW, HIGH }
