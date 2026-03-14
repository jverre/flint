/*
 * tcp-relay.c — Pre-spawned shell with TCP accept loop.
 *
 * At startup: allocates a PTY, forks /bin/sh -i on the slave side.
 * Listens on TCP :5000, accepts one connection at a time,
 * relays data between the TCP socket and PTY master via poll().
 * On disconnect the socket is closed and we loop back to accept() —
 * the shell stays alive across connections.
 *
 * Build: musl-gcc -static -O2 -o tcp-relay tcp-relay.c -lutil
 */

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pty.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define PORT 5000
#define BUFSZ 4096

int main(void) {
    signal(SIGPIPE, SIG_IGN);

    /* Allocate PTY and fork shell */
    int master;
    pid_t pid = forkpty(&master, NULL, NULL, NULL);
    if (pid < 0) { perror("forkpty"); return 1; }
    if (pid == 0) {
        /* child — exec shell */
        execl("/bin/sh", "sh", "-i", NULL);
        perror("exec");
        _exit(1);
    }

    /* parent — set up TCP listener */
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("socket"); return 1; }

    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(PORT),
        .sin_addr.s_addr = 0,  /* INADDR_ANY */
    };
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); return 1;
    }
    if (listen(srv, 1) < 0) { perror("listen"); return 1; }

    /* Accept loop — shell persists across connections */
    for (;;) {
        int conn = accept(srv, NULL, NULL);
        if (conn < 0) continue;

        setsockopt(conn, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));

        struct pollfd fds[2] = {
            { .fd = conn,   .events = POLLIN },
            { .fd = master, .events = POLLIN },
        };

        char buf[BUFSZ];
        int done = 0;
        while (!done) {
            int n = poll(fds, 2, -1);
            if (n < 0) { if (errno == EINTR) continue; break; }

            /* TCP → PTY */
            if (fds[0].revents & (POLLIN | POLLHUP)) {
                ssize_t r = read(conn, buf, BUFSZ);
                if (r <= 0) { done = 1; break; }
                write(master, buf, r);
            }
            /* PTY → TCP */
            if (fds[1].revents & (POLLIN | POLLHUP)) {
                ssize_t r = read(master, buf, BUFSZ);
                if (r <= 0) { done = 1; break; }
                if (write(conn, buf, r) <= 0) { done = 1; break; }
            }
        }
        close(conn);
    }
}
