# Broforce-Source

This repo contains a Python script, `decompile.py`, and its folder rules, `layout-map.txt`, but none of the game's code. The script generates the game's C# source tree on your own machine from your own copy of Broforce, by decompiling the game's `Assembly-CSharp.dll` into `Source/`.

## Requirements

- Windows x64 or Linux x64.
- Python 3.9 or later to run the script. It only uses the standard library, so it doesn't need anything else.
- Network access. The first run downloads ilspycmd (ILSpy's command-line decompiler) version 11.2.0.10050 from NuGet and checks the file against a SHA512 pinned in the script. It gets cached and later runs reuse it. The cache folder is `%LOCALAPPDATA%\broforce-source` on Windows and `~/.cache/broforce-source` on Linux, or `$XDG_CACHE_HOME/broforce-source` if that's set. The docs are also downloaded on every run unless you use `--docs` or `--no-docs` (see [Usage](#usage)).
- An installed copy of the game. The script reads the install's `Managed` folder.
- macOS isn't supported since ilspycmd doesn't have a standalone macOS build, so the script just exits with a message there.
- A .NET SDK and VS Code with the C# extension, to build and navigate the output. You don't need these to run the script.

## Usage

```
git clone https://github.com/alexneargarder/Broforce-Source.git
cd Broforce-Source
python3 decompile.py
```

On Windows the Python command may be `py` or `python`.

The output always goes to `Source/` next to the script, no matter which folder you run it from.

By default the script finds the game through the Steam libraries listed in Steam's `libraryfolders.vdf`. On Windows it gets Steam's location from the registry. On Linux it checks native Steam (`~/.local/share/Steam` or `~/.steam/steam`) and Flatpak Steam. Inside the install folder it uses `Broforce_beta_Data/Managed` or `Broforce_Data/Managed`, whichever one has `Assembly-CSharp.dll`.

Options:

- `--game DIR`: the install folder, if your copy isn't in a Steam library or the Steam search doesn't find it.
- `--dll FILE`: decompiles a different `Assembly-CSharp.dll`. The game's other DLLs, which the decompiler and the project reference, still come from the install's `Managed` folder, found the same way as above or from `--game`.
- `--docs FILE`: inlines a local `Assembly-CSharp.xml` instead of downloading one.
- `--no-docs`: doesn't inline any documentation.

You can't use `--docs` and `--no-docs` together.

By default the script downloads `Assembly-CSharp.xml` from [Broforce-Docs](https://github.com/alexneargarder/Broforce-Docs), and the decompiler writes it into the source as `///` comments. VS Code shows them on hover.

After a run, `Source/` has:

- The decompiled classes. Classes in the global namespace are sorted into folders by `layout-map.txt` (see [Folder layout](#folder-layout)). Any that don't match a rule go in `Unsorted/`, and the script prints how many there were.
- Namespaced types, in a folder for each namespace the way the decompiler writes them, for example `Rewired/Platforms/Switch/`.
- `Assembly-CSharp.csproj`, the project, at the root of `Source/`. It includes every folder.
- `Directory.Build.props`, which has the path of the install's `Managed` folder. The project references the game's DLLs through it.
- `Assembly-CSharp.il`, the IL of the whole DLL (see [IL dump](#il-dump)).
- `BUILD.md`, with the DLL's SHA256 and MVID, the ilspycmd version and flags, whether docs were inlined and the XML's SHA256, and whether the DLL is the reference build.

Rerunning the script overwrites the previous output. It also deletes files an earlier run wrote that this run didn't, which are tracked in `Source/.layout-manifest`, and removes any folders left empty. It only deletes files the script wrote itself.

Broforce-Docs is written against one build of the game, the reference build, with the MVID `4952e11f-34bd-45eb-9d34-a304cfbdda7b`. If your DLL is from a different build the tree is still written, but the script prints a warning and `BUILD.md` includes a warning that the code may be different from what the docs describe.

## Opening the source in VS Code

You need a .NET SDK and the C# extension. Open the `Source/` folder in VS Code and the extension loads `Assembly-CSharp.csproj`, you don't need a `.sln`. The project targets .NET Framework 3.5, so the first load restores the .NET Framework 3.5 reference assemblies over the network. After that, go to definition and find all references work across the whole game.

If you build the project with `dotnet build` in `Source/`, you'll get three errors and no others. They come from three `get_delegates` property getters in `Rewired/Platforms/Switch/NintendoSwitchInputManager.cs`, which is Rewired's Nintendo Switch input code. This version of ilspycmd can't decompile them, so their bodies are left as an error comment. Navigation isn't affected.

## IL dump

`Source/Assembly-CSharp.il` has the IL of every method in the DLL, for writing Harmony transpilers. Each method ends with a line like this:

```
} // end of method SkinnedMook::Start
```

To find a method, search the file for `end of method <Type>::<Method>`. Its IL is directly above that line.

`<Type>` is just the type's own name, without the namespace or the outer class, so a nested type's getter is `UserData::get_delegates`. A generic type keeps its number of type parameters after a backtick, like ``AssetBundleReference`1::.ctor``, and compiler-generated names are in single quotes, like `'<UpdateComponent>c__AnonStorey0'::.ctor`. Property getters and setters are `get_<Name>` and `set_<Name>`, constructors are `.ctor` and static constructors are `.cctor`. An overloaded method matches once for each overload. Classes in different namespaces, or nested inside different classes, can share a name, so a search can also match more than one class: the nearest unindented `.class` line above a match names the top-level class it's in, with its namespace. A coroutine's code is in the `MoveNext` method of the iterator class it creates, which the coroutine's own IL names on a `newobj` line.

## Folder layout

Only classes in the global namespace are moved, into folders one level deep by the rules in `layout-map.txt`, and namespaced types stay where the decompiler puts them. Moving a file doesn't change what's in it. The rule kinds (`class`, `base`, `prefix`) and their order are listed at the top of `layout-map.txt`.

## Docs for coding agents

The root `AGENTS.md` has the rules for working in the generated tree and for searching the IL dump, plus a list of the folder docs, which are the `AGENTS.md` files in eight of the `Source/` folders. Each folder doc covers what's hard to get from the code alone in that part of the game (entry points, flows that cross into other folders, RPCs the game drops without logging anything). They're written against the reference build, and the script never changes them. Claude Code (v2.1.277 or later) reads the root `AGENTS.md` when a session starts in the repo, and reads a folder's `AGENTS.md` when it opens a file in that folder with its Read tool. If it reads code through shell commands instead, it only gets a folder doc by opening it, and the root file's list says when to open each one. Claude Code only reads `AGENTS.md` files when there's no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` in the folder you start it in or any folder above it. If you have one, you can set Project instructions to `claude-md-and-agents-md` in `/config` to have it read both.

## Push guard

If your copy of this repo has decompiled output committed, for example on a private branch, you can use `hooks/pre-push`, a git pre-push hook. It allows any push to the remote whose URL matches the git config key `pushguard.giteaUrl`. Any other remote only gets pushes to `master`, and only when the pushed commit has no `.cs` or `.il` file in its history. Deleting `master` there is refused too.

To install it, run this from the repo root:

```
cp hooks/pre-push "$(git rev-parse --git-common-dir)/hooks/pre-push"
git config pushguard.giteaUrl <url of your private remote>
```

The URL has to match the push URL `git remote -v` shows for that remote exactly.
