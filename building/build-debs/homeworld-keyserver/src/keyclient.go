package main

import (
	"keyclient"
	"log"
	"keyclient/setup"
	"os"
)

// the keyclient is a daemon with a few different responsibilities:
//  - perform initial token authentication to get a keygranting certificate
//  - generate local key material
//  - renew the keygranting certificate
//  - renew other certificates

func main() {
	logger := log.New(os.Stderr, "[keyclient] ", log.Ldate | log.Ltime | log.Lmicroseconds | log.Lshortfile)
	_, err := setup.LoadAndLaunch("/etc/hyades/keyclient/keyclient.conf", logger)
	if err != nil {
		logger.Fatal(err)
	}
}
