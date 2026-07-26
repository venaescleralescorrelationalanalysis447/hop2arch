# The Hopfile (`hopfile.json`) — v1

A **hopfile** is a single JSON document that describes everything `hop` needs to
know about the machine you are leaving. It is produced on Windows by
`windows/hop-scan.ps1`, consumed on Arch by `hop plan` / `hop land`, and is meant
to be readable, diffable and safe to paste into a GitHub issue *after*
`hop scrub` has removed the personal bits.

Everything except `hopfile_version`, `generated_at` and `generator` is optional:
the scanner degrades gracefully (no admin rights, no Steam, no WSL…) and the
Python side treats missing keys as "unknown", never as an error.

Alongside the hopfile the scanner may write a **payload directory** (default
`hop-payload/`) with the actual bytes of the things worth carrying over: SSH
keys, Wi-Fi passwords, browser bookmarks, terminal config, wallpapers. The
hopfile only references them by relative path.

## Top level

| Key | Type | Meaning |
| --- | --- | --- |
| `hopfile_version` | int | Always `1` for this document. |
| `generated_at` | string | ISO-8601 UTC timestamp. |
| `generator` | string | e.g. `hop-scan.ps1/0.1.0`. |
| `payload_dir` | string\|null | Relative path to the payload directory, or null. |
| `system` | object | Hardware, firmware, locale, timezone. |
| `disks` | array | Physical disks and their partitions. |
| `user` | object | The Windows user account and its data folders. |
| `software` | array | Everything installed, one entry per program. |
| `dev` | object | Developer environment. |
| `browsers` | array | Installed browsers and profiles. |
| `network` | object | Wi-Fi profiles, hostname. |
| `gaming` | object | Steam / Epic / GOG libraries. |
| `personalization` | object | Wallpaper, theme, accent colour, user fonts. |
| `payload` | object | Index of files inside `payload_dir`. |
| `warnings` | array of string | Things a human must look at (BitLocker, RAID…). |

## `system`

```jsonc
{
  "hostname": "DESKTOP-4T1KQ9",
  "windows": {
    "caption": "Microsoft Windows 11 Pro",
    "version": "10.0.22631",
    "build": "22631",
    "edition": "Professional",
    "install_date": "2023-11-04T09:12:00Z"
  },
  "firmware": "UEFI",              // "UEFI" | "BIOS" | "unknown"
  "secure_boot": true,             // bool | null when not detectable
  "tpm": true,
  "locale": "ru-RU",               // user locale
  "ui_language": "en-US",
  "keyboard_layouts": ["00000409", "00000419"],   // raw Windows KLIDs
  "timezone": {
    "windows": "Russian Standard Time",
    "iana": "Europe/Moscow",       // resolved by the scanner when possible
    "utc_offset_minutes": 180
  },
  "cpu": { "name": "AMD Ryzen 7 5800X", "vendor": "AuthenticAMD", "cores": 8, "threads": 16 },
  "memory_gb": 32,
  "gpus": [
    { "name": "NVIDIA GeForce RTX 3070", "vendor": "nvidia", "driver_version": "552.44" }
  ],
  "chassis": "desktop",            // "desktop" | "laptop" | "vm" | "unknown"
  "battery": false
}
```

`gpus[].vendor` is normalised to one of `nvidia`, `amd`, `intel`, `other` —
`hop plan` uses it to pick the right driver packages.

## `disks`

```jsonc
[
  {
    "index": 0,
    "model": "Samsung SSD 980 PRO 1TB",
    "size_bytes": 1000204886016,
    "bus": "NVMe",
    "partition_style": "GPT",
    "system_disk": true,
    "partitions": [
      {
        "letter": "C",
        "label": "OS",
        "fs": "NTFS",
        "size_bytes": 900000000000,
        "free_bytes": 320000000000,
        "bitlocker": "off",        // "on" | "off" | "unknown"
        "kind": "basic"            // "efi" | "recovery" | "reserved" | "basic"
      }
    ]
  }
]
```

## `user`

```jsonc
{
  "name": "vasya",
  "full_name": "Vasya Pupkin",
  "profile_path": "C:\\Users\\vasya",
  "folders": {
    "Desktop":   { "path": "C:\\Users\\vasya\\Desktop", "size_bytes": 123456, "files": 42 },
    "Documents": { "...": "..." },
    "Downloads": { "...": "..." },
    "Pictures":  { "...": "..." },
    "Music":     { "...": "..." },
    "Videos":    { "...": "..." }
  },
  "onedrive": { "present": true, "path": "C:\\Users\\vasya\\OneDrive", "size_bytes": 0 }
}
```

## `software`

One entry per installed program. Duplicates across sources are collapsed by the
scanner; `sources` keeps the provenance.

```jsonc
[
  {
    "name": "Mozilla Firefox (x64 ru)",
    "version": "128.0",
    "publisher": "Mozilla",
    "sources": ["registry", "winget"],   // registry | winget | store | choco | scoop
    "winget_id": "Mozilla.Firefox",      // null when unknown
    "install_location": "C:\\Program Files\\Mozilla Firefox",
    "size_bytes": 0,                     // 0 when unknown
    "system_component": false            // true for redistributables, update helpers…
  }
]
```

## `dev`

```jsonc
{
  "git":  { "present": true, "user_name": "vasya", "user_email": "v@example.com", "default_branch": "main" },
  "ssh_keys": [ { "file": "id_ed25519", "type": "ed25519", "encrypted": true, "public_key": "ssh-ed25519 AAAA… vasya@desktop" } ],
  "gpg":  { "present": false, "key_ids": [] },
  "wsl":  { "present": true, "distros": [ { "name": "Ubuntu-22.04", "version": 2, "default": true } ] },
  "vscode": { "present": true, "flavor": "code", "extensions": ["ms-python.python"], "settings": true },
  "runtimes": { "node": "22.3.0", "python": "3.12.4", "dotnet": "8.0.7", "java": null, "go": null, "rustup": "1.79.0" },
  "shell": { "powershell_profile": true, "windows_terminal": true }
}
```

Private key material is **never** put in the JSON. When the user opts in with
`-WithSecrets` the private key files are copied into the payload directory and
referenced from `payload.entries`.

## `browsers`

```jsonc
[
  {
    "id": "chrome",                  // chrome | edge | firefox | brave | vivaldi | opera | other
    "name": "Google Chrome",
    "default": true,
    "profiles": [ { "name": "Default", "path": "C:\\Users\\…\\User Data\\Default" } ],
    "bookmark_count": 512
  }
]
```

## `network`

```jsonc
{
  "hostname": "DESKTOP-4T1KQ9",
  "wifi_profiles": [ { "ssid": "home-5g", "auth": "WPA2PSK", "has_secret": true } ],
  "hosts_entries": 3
}
```

`has_secret` means the password was exported into the payload (requires
`-WithSecrets` **and** an elevated shell).

## `gaming`

```jsonc
{
  "steam": {
    "present": true,
    "libraries": ["C:\\Program Files (x86)\\Steam\\steamapps"],
    "games": [ { "appid": 730, "name": "Counter-Strike 2", "size_bytes": 32000000000 } ]
  },
  "epic": { "present": false, "games": [] },
  "gog":  { "present": false, "games": [] }
}
```

## `personalization`

```jsonc
{
  "wallpaper": "C:\\Users\\vasya\\AppData\\Roaming\\…\\img0.jpg",
  "theme": "dark",                 // "dark" | "light" | "unknown"
  "accent_color": "#0078D4",
  "fonts_user": ["JetBrainsMono-Regular.ttf"]
}
```

## `payload`

```jsonc
{
  "entries": [
    { "kind": "ssh",      "path": "ssh/id_ed25519",        "restore_to": "~/.ssh/id_ed25519",              "mode": "0600" },
    { "kind": "wifi",     "path": "wifi/home-5g.json",     "restore_to": null,                             "mode": "0600" },
    { "kind": "bookmarks","path": "browsers/chrome.html",  "restore_to": null,                             "mode": "0644" },
    { "kind": "wallpaper","path": "personalization/wall.jpg","restore_to": "~/Pictures/wallpaper.jpg",     "mode": "0644" },
    { "kind": "font",     "path": "fonts/JetBrainsMono-Regular.ttf", "restore_to": "~/.local/share/fonts/JetBrainsMono-Regular.ttf", "mode": "0644" }
  ]
}
```

`kind` is one of `ssh`, `gpg`, `wifi`, `bookmarks`, `gitconfig`, `terminal`,
`vscode`, `wallpaper`, `font`, `other`. `restore_to` uses `~` for the new user's
home; `null` means "`hop land` decides / imports it a smarter way".

## Privacy

`hop scrub hopfile.json` produces a version safe to share: hostnames, user names,
e-mails, SSIDs, public keys, wallpaper paths and drive labels are replaced with
stable fake values, and the payload index is dropped. The mapping is
deterministic per-run so a scrubbed file still makes sense to read.
