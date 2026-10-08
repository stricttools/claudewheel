package main

import (
	"runtime/debug"
	"strings"
)

// Version is set by ldflags at build time: -X main.Version=x.y.z. The name
// must stay exported and spelled this way, because the linker silently does
// nothing when the symbol is absent. Without it, the module version from the
// build information is used, or "dev" for a build from a working tree.
var Version = ""

func init() {
	if Version != "" {
		Version = strings.TrimPrefix(Version, "v")
		return
	}
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "(devel)" {
		Version = strings.TrimPrefix(info.Main.Version, "v")
	} else {
		Version = "dev"
	}
}
