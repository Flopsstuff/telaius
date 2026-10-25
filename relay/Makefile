# Cross-build inside Docker:  docker run --rm -v "$PWD":/src telaius-armbuild make
CC = arm-linux-gnueabihf-gcc
CFLAGS = -O2 -Wall -Wextra -march=armv7-a -mfpu=neon -mfloat-abi=hard
LDFLAGS = -static -pthread
LDLIBS = -lm

telaius-relay: telaius-relay.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS) $(LDLIBS)
	arm-linux-gnueabihf-strip $@

clean:
	rm -f telaius-relay
