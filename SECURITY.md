# Security

workplane is a single-user tool. It trusts everyone who can reach its port and everything you point an agent at. Read this before you expose the server or start an agent.

## Agent runs

`work run` starts a coding agent on your machine, in a git worktree, with your user's permissions: your home directory, your `gh` login and every secret in the environment of the shell that ran `work run`. The agent pushes a branch and opens a PR with your credentials. The server itself never starts a process and never writes to GitHub.

- An unattended agent cannot answer approval prompts, so `work run` refuses to start until `approval_mode` is set in `[runner]`. For omp that means `"yolo"`: no prompts. Setting it is you accepting everything on this page.
- Issue and comment text is untrusted input and goes into the agent's prompt. A hostile comment can steer an agent that holds your token. Only run agents on issues you have read.
- Nothing isolates a run from the rest of your machine yet. That is the [isolation roadmap](https://github.com/seandavi/workplane/issues?q=is%3Aissue+label%3Aisolation). Until it lands, work with repositories you don't control under a separate OS user or in a VM.

## The network surface

- The server listens on `127.0.0.1` unless you set `HOST`. `HOST=0.0.0.0`, a reverse proxy or `tailscale serve` exposes it to everything that can reach the port.
- There is no authentication. Anyone who can reach the port can read every item title, including those from private repositories, and can change the queue. They cannot start an agent: runs are started by the `work` CLI on a machine you control, never by the server. Rely on loopback or your tailnet's access rules until [API authentication](https://github.com/seandavi/workplane/issues?q=is%3Aissue+label%3Aroadmap) lands.
- A form post that another website made your browser send is refused: its `Origin` must be the server's own, or listed in `allowed_origins`. Redirect targets taken from forms must be paths on this server.

## Reporting a vulnerability

Report it privately through GitHub: on the repository page, **Security → Report a vulnerability**. Please include how to reproduce it. Only the latest release is supported.
