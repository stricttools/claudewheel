// Command gendocs rewrites the tables the guardrails documentation page
// generates from the guardrail model. scripts/gen-guardrail-docs runs it.
package main

import (
	"fmt"
	"os"

	"github.com/stricttools/claudewheel/internal/guardrail"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: gendocs <guardrails-page>")
		os.Exit(2)
	}
	path := os.Args[1]
	data, err := os.ReadFile(path)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	page, err := guardrail.RewriteDocTables(string(data))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %s: %v\n", path, err)
		os.Exit(1)
	}
	if page == string(data) {
		fmt.Printf("%s: tables already match the model\n", path)
		return
	}
	if err := os.WriteFile(path, []byte(page), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	fmt.Printf("%s: tables rewritten from the model\n", path)
}
