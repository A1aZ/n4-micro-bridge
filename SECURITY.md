# Security and privacy

The control service binds to loopback only. Do not expose it through port forwarding, a reverse proxy or a public tunnel. It is a local control interface, not an authenticated multi-user service. Another process on your computer can call its APIs.

Physical or simulated Micro input can approve/reject actions, switch chats or send messages according to host bindings. Screen diagnostics are visual-only; hardware keys remain active. Never test input while a sensitive approval prompt is open.

Logs may contain local paths, hardware identifiers, bindings and raw reports. Review/redact them before sharing. Do not upload data/config.json, logs, certificates or complete portable folders as bug reports.

Driver install and certificate trust are separate privileged steps. Never turn off Windows security features as a routine installation workaround. Local development signing is not a production signing solution.

To report a vulnerability, privately contact the repository maintainer via the hosting platform's private reporting facility if enabled. Do not publish exploit payloads or credentials in an ordinary issue. Maintainers must configure a working security contact before public release.
