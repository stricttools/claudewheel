package deletion

import (
	"fmt"
	"strings"

	"github.com/stricttools/claudewheel/internal/archiver"
	"github.com/stricttools/claudewheel/internal/tui/widgets"
)

// SafermExplanation is what a page says when saferm is missing or lacks a
// feature: what is wrong with it, then what deleting profile name without it
// would cost, wrapped for a page.
func SafermExplanation(missing *archiver.Unavailable, name string) []string {
	lines := widgets.WrapText(missing.Diagnosis(), widgets.PageTextWidth)
	lines = append(lines, "")
	return append(lines, widgets.WrapText(missing.Stakes(name), widgets.PageTextWidth)...)
}

// SafermInstallOffer is the confirmation offering to install saferm so that
// profile name can be deleted: y installs it, n and Escape cancel the
// deletion. The bar and profile delete both ask it.
func SafermInstallOffer(missing *archiver.Unavailable, name string) widgets.Confirmation {
	return widgets.Confirmation{
		Title:   fmt.Sprintf("Cannot delete '%s' without saferm", name),
		Lines:   SafermExplanation(missing, name),
		Accept:  strings.ToLower(missing.Verb()) + " saferm from its published release",
		Decline: "cancel the deletion",
		Skip:    "cancel the deletion",
	}
}
