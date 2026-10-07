package svc

import "animals"

type Store struct {
	items []string
}

func (s Store) Add(item string) {
	s.items = append(s.items, item)
	animals.NewDog(item)
}

func (s *Store) Len() int { return len(s.items) }

func Run() {
	s := Store{}
	s.Add("x")
	fmt.Println(s.Len())
}
