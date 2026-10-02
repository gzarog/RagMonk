package animals

import (
	"fmt"
	"strings"
)

type Animal interface {
	Speak() string
}

type Dog struct {
	Name string
}

func (d *Dog) Speak() string {
	return fmt.Sprintf("%s says woof", d.Name)
}

func NewDog(name string) *Dog {
	d := &Dog{Name: strings.ToUpper(name)}
	return d
}
