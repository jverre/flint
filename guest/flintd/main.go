package main

import (
	"fmt"
	"log"
	"net/http"
	"time"
)

var startTime = time.Now()

func main() {
	mux := newServer()
	addr := ":5000"
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	fmt.Printf("flintd listening on %s\n", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("flintd: %v", err)
	}
}
