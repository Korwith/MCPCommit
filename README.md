# MCPCommit
Shared actions script that can build Minecraft mods via GitHub repo\
Utilizes a configuration file to figure out what to do\

Reads `omnimcp.json` from the GitHub Repo\
Downloads the matching MCP release (from archive.org) and vanilla jars\
Decompiles the vanilla jar, applies patches, recompiles, reobfuscates\
Uploads the finished mod file as an artifact (found in actions)\


```yaml
# .github/workflows/build.yml
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Korwith/MCPCommit@main
        with:
          config: omnimcp.json
```

### omnimcp.json

```json
{
  "name": "MyMod",
  "version": "1.0",
  "minecraft": "b1.7.3",
  "sources":   {"client": "src/client"},
  "resources": {"client": "resources/client"}
}
```

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Jar base name |
| `version` | `dev` | Mod version, used in file names. Or `{"file": "src/.../mod_X.java", "regex": "return \"([^\"]+)\""}` to read it from a source file (first group) |
| `minecraft` | required | Game version, as MCP names it (`a1.2.6`, `b1.7.3`, `1.5.2`, `1.12`, `13w09c`...) |
| `mcp` | newest for that version | A specific pack, e.g. `mcp70` instead of `mcp70a` |
| `sides` | sides that have sources/patches | `["client"]`, `["server"]` or both |
| `sources` | | Per side: a folder of `.java` files laid out like the decompiled source (`net/minecraft/src/BlockPiston.java`). They replace or add to the decompiled game. This is what Workbench's *Mod source code* export contains |
| `patches` | | Per side: a `.patch` file or folder of them (or a list of those), unified diffs applied with `patch -p1` in the side's source root (`diff -ruN a/ b/`). Lets you avoid committing whole decompiled Mojang files |
| `resources` | | Per side: files copied into the mod jar as-is (textures, sounds) |
| `jar_mods` | | Per side: zips/jars merged into the vanilla jar *before* decompiling, for mods built on ModLoader etc. Each is a path, or `{"url": ..., "sha256": ...}` to download it (cached; the checksum is optional but recommended) |
| `libraries` | | Per side: a folder of `.java` files (or a list of folders) compiled with the mod but **not** shipped (stubs or APIs of other mods, e.g. the sources of the mod yours is a plugin for) |
| `exclude` | | Class names or globs (`net/minecraft/src/Tessellator`) compiled but not shipped, e.g. vanilla classes a patch only opens up for compiling. Inner classes follow their outer class |
| `filename` | `{name}-{version}-mc{minecraft}-{side}.jar` | Name of the mod file; `{name}`, `{version}`, `{minecraft}`, `{side}` are filled in. A `.zip` name suits ModLoader's `mods/` folder |
| `java` | `8` | JDK major version used in CI |
| `patched_jar` | `false` | Also produce the whole game jar with the mod installed |
| `sources_zip` | `true` | Also produce a zip of the mod's changed/new sources |
| `mcp_url` | archive.org | Download the pack zip from here instead |

Action outputs: `out-dir`, `artifact-name`, `pack`, `version` and `mod-file` (path of the built mod file).

All paths are relative to `omnimcp.json`.