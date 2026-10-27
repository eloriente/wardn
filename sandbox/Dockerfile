# Dev sandbox: a Debian that behaves like a small server (systemd + sshd).
# The agent connects to it over SSH, exactly as it would to the homelab.
FROM debian:12-slim

ENV container=docker
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        systemd systemd-sysv dbus openssh-server sudo \
        procps iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user the agent logs in as. Only the public key is baked in.
ARG SSH_PUBKEY
RUN test -n "$SSH_PUBKEY" \
    && useradd --create-home --shell /bin/bash wardn \
    && install -d -m 700 -o wardn -g wardn /home/wardn/.ssh \
    && echo "$SSH_PUBKEY" > /home/wardn/.ssh/authorized_keys \
    && chown wardn:wardn /home/wardn/.ssh/authorized_keys \
    && chmod 600 /home/wardn/.ssh/authorized_keys

# Key-only SSH, no root login.
RUN mkdir -p /run/sshd \
    && printf 'PasswordAuthentication no\nPermitRootLogin no\n' > /etc/ssh/sshd_config.d/wardn.conf \
    && systemctl enable ssh

# Fake docker, so container tools can be developed without real containers.
COPY fake/docker /usr/bin/docker
COPY fake/docker-ps.jsonl /etc/wardn-fake/docker-ps.jsonl
RUN chmod 755 /usr/bin/docker \
    && echo 'wardn ALL=(root) NOPASSWD: /usr/bin/docker ps -a --format {{json .}}' > /etc/sudoers.d/wardn-docker \
    && chmod 440 /etc/sudoers.d/wardn-docker \
    && visudo -cf /etc/sudoers.d/wardn-docker

EXPOSE 22

# systemd must be PID 1; SIGRTMIN+3 is its clean-shutdown signal.
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
