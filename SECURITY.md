# Security

hop2arch formats a USB stick, repartitions a disk and installs an operating system over somebody's
Windows. The worst outcome is not a crash — it is the wrong disk. This file says what the project
assumes, what it defends against, and how to report something it got wrong.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on
<https://github.com/Ramirmir/hop2arch>. Please do not open a public issue first.

Include what you would need yourself: the input that triggers it, what you expected, and what
happened. If it involves a hopfile or a plan, run it through `hop scrub` before attaching it — and
note that scrubbing is not anonymisation, only redaction of the obvious fields.

This is a student project with one maintainer and no service behind it, so there is no response-time
guarantee. Expect a reply in days rather than hours.

## The threat model

**Untrusted inputs.** Three documents drive hop, and none of them is trusted:

- `hopfile.json` — produced by the scanner, but plain JSON that can be edited or handed to you.
- `hop-plan.json` — the project explicitly invites you to edit it before landing.
- Anything on the install medium, which is FAT32 and writable by anyone who has it.

Everything read from those goes through validation before it reaches a command, a file path or a
generated script. Values that end up in a generated shell or PowerShell script are checked against a
character set rather than escaped, because a rejected value is easier to reason about than an escaped
one.

**Trusted:** the machine hop runs on, the account running it, and the person answering the prompts.
hop does not defend against its own user, and it does not try to protect a machine whose operator is
hostile.

**Partly trusted:** the Arch mirrors. hop fetches over HTTPS only, refuses to follow a redirect off
TLS, checks the ISO's checksum always and its GPG signature when `gpg` is available. A checksum
served by the same mirror as the image proves the transfer was not corrupted and nothing more; only
the signature ties the bytes to Arch. `VerifyResult` keeps those two facts apart and never reports a
verified image on a checksum alone. A signature gpg *rejects* stops the run outright; a signature it
could not check is a question the user is asked.

## What is in scope

- A path that lets an edited hopfile or plan reach a device, a file outside the intended tree, or a
  generated script unchecked.
- Anything that could select the wrong disk: a refusal that can be bypassed, an identity check that
  can be satisfied by the wrong drive, a window between the confirmation and the write.
- A private key, a Wi-Fi password or any payload content reaching stdout, a log, the plan JSON or a
  scrubbed hopfile.
- `hop scrub` leaving an identifying field behind that it claims to remove.
- The scanner writing outside the two paths named on its command line.

## What is out of scope

- **The USB stick has no protection at all.** It is FAT32, which has no permissions, and with
  `--with-secrets` it carries private SSH keys and Wi-Fi passwords in the clear. hop says this in the
  transcript before it writes. Physical possession of that stick is game over by design; the answer
  is to keep it and erase it afterwards, not to encrypt it.
- **`hop scrub` is redaction, not anonymity.** An unusual combination of installed software, disk
  sizes and game library identifies a machine on its own. The module's own docstring says so.
- **A Wi-Fi PSK is visible in `/proc/<pid>/cmdline` for the duration of one `nmcli` call.** That is
  nmcli's interface. The alternative is hop hand-writing NetworkManager's keyfile format, where a
  malformed file silently breaks networking on a machine that has just lost its other operating
  system. The trade was made deliberately; the key never reaches the transcript.
- Denial of service against a local tool the user chose to run.
- Anything requiring an attacker who already has administrator rights on the machine.

## Known accepted risks

| Risk | Why it is accepted |
| --- | --- |
| Secrets unprotected on FAT32 | Disclosed before writing; encryption would make the stick unbootable |
| PSK on the nmcli command line | The alternative risks silently breaking networking; not in the transcript |
| Scrub does not anonymise | Stated in the module and in `CONTRIBUTING.md`; the alternative is a useless bug report |
| `script=` autostart depends on archiso | If it stops working the stick still boots and prints the manual command |

## The honest part

`hop go`, `hop install` and `hop land --execute` have never been run against real hardware. They are
unit-tested against injected fakes and have been through an adversarial audit aimed specifically at
data loss, which found and fixed defects including one that would have erased any USB stick under
32 GB before failing to format it. That is not the same as a real run, and the first person to try it
is the first person to try it.

## Supported versions

Version 0.1.0 is the only release. Fixes go to `main`.
