FROM --platform=$TARGETPLATFORM golang:1.24-alpine AS builder
WORKDIR /src
COPY guest/flintd/ .
RUN go mod tidy && CGO_ENABLED=0 go build -ldflags='-s -w' -o /flintd .

FROM --platform=$TARGETPLATFORM alpine:3.21
RUN apk add --no-cache iproute2
COPY --from=builder /flintd /usr/local/bin/flintd
COPY assets/init-net.sh /etc/init-net.sh
RUN chmod +x /usr/local/bin/flintd /etc/init-net.sh && mkdir -p /workspace
