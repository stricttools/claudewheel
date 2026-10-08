package cli

import (
	"github.com/stricttools/strictcli/go/strictcli"

	"github.com/stricttools/claudewheel/internal/profiles"
)

// categoryHelp says what each permission category holds, for the choices of
// permission list --category.
var categoryHelp = map[string]string{
	"allow": "the rules Claude Code follows without asking",
	"deny":  "the rules Claude Code refuses",
	"ask":   "the rules Claude Code asks about first",
}

// listFormats are the human renderings of permission list.
const (
	formatGrouped = "grouped"
	formatFlat    = "flat"
)

// registerPermission adds the permission group. add and remove edit the
// allow list only: the launch resets deny and ask to the canonical lists, so
// a hand-added deny or ask rule would be undone.
func registerPermission(app *strictcli.App) {
	g := app.Group("permission",
		"add and remove allow rules, and list permission rules, across Claude profiles")

	addTarget := newProfileTarget("add the rule to every registered profile at once")
	mutatingCommand(g, "add",
		"add a rule to the allow list of a profile's settings.json, such as Bash"+
			" or Read(//home/**). Use --profile to target a single profile or"+
			" --all-profiles to add it to every registered profile. A profile that"+
			" already allows the rule is left unchanged. Only allow is edited:"+
			" every launch resets deny and ask to the canonical guardrail lists. A"+
			" rule the guardrail lists as an allow conflict (one patch-profiles"+
			" would remove again) is refused",
		func(c *call, kw map[string]interface{}) error {
			profile, all := addTarget.read(kw)
			return permissionAdd(c, profile, all, kwString(kw, "rule"))
		},
		strictcli.WithArgs(strictcli.NewArg("rule",
			"permission rule string to allow (e.g. Bash, Read(//home/**), Edit)", strictcli.ArgRequired())),
		strictcli.WithFlags(addTarget.flag))

	removeTarget := newProfileTarget("remove the rule from every registered profile at once")
	mutatingCommand(g, "remove",
		"remove a rule from the allow list of a profile's settings.json, by its"+
			" exact string. Use --profile to target a single profile or"+
			" --all-profiles to remove it from every registered profile. Reports"+
			" for each profile whether the rule was found; a profile without it is"+
			" left unchanged",
		func(c *call, kw map[string]interface{}) error {
			profile, all := removeTarget.read(kw)
			return permissionRemove(c, profile, all, kwString(kw, "rule"))
		},
		strictcli.WithArgs(strictcli.NewArg("rule",
			"exact permission rule string to remove from allow (must match an existing entry)", strictcli.ArgRequired())),
		strictcli.WithFlags(removeTarget.flag))

	listTarget := newProfileTarget("list the rules of every registered profile")
	categories := make([]strictcli.ChoiceValue, 0, len(profiles.PermissionCategories()))
	for _, category := range profiles.PermissionCategories() {
		categories = append(categories, strictcli.Ch(category, categoryHelp[category]))
	}
	readOnlyCommand(g, "list",
		"list the permission rules of a profile's settings.json in the format"+
			" --format names. Use --category to list a single category. Use"+
			" --profile to inspect a single profile or --all-profiles to show the"+
			" rules of every registered profile, each under a header; a profile"+
			" without a settings.json is listed with no rules. The"+
			" framework-owned --json answers a machine instead: one envelope"+
			" carrying every listed profile, whatever --format the human form"+
			" would have used",
		func(c *call, kw map[string]interface{}) error {
			profile, all := listTarget.read(kw)
			category, _ := kwOptString(kw, "category")
			return permissionList(c, profile, all, kwString(kw, "format"), category)
		},
		strictcli.WithFlags(
			listTarget.flag,
			strictcli.StringFlag("format", "output format: grouped (indented tree) or flat (tsv)", strictcli.Required(),
				strictcli.Choices(
					strictcli.Ch(formatGrouped, "one indented block per category, one rule per line"),
					strictcli.Ch(formatFlat, "one tab-separated category-and-rule pair per line"))),
			strictcli.StringFlag("category",
				"list only this permission category; when omitted, every category is listed",
				strictcli.Optional(), strictcli.Choices(categories...))),
		strictcli.PayloadSchema(permissionListSchema()))
}

// permissionListSchema is the shape permission list answers a machine with:
// an object of profiles, each a name and its permission categories. The
// permissions object requires no member because --category narrows it to
// one category; being closed is what says an unexpected key never appears.
func permissionListSchema() map[string]interface{} {
	rules := strictcli.SchemaArray(strictcli.SchemaType("string"))
	categories := map[string]interface{}{}
	for _, category := range profiles.PermissionCategories() {
		categories[category] = rules
	}
	profile := strictcli.SchemaObject(map[string]interface{}{
		"name":        strictcli.SchemaType("string"),
		"permissions": strictcli.SchemaObject(categories, nil, false),
	}, []string{"name", "permissions"}, false)
	return strictcli.SchemaObject(map[string]interface{}{
		"profiles": strictcli.SchemaArray(profile),
	}, []string{"profiles"}, false)
}

// permissionTargets opens the workspace and resolves the profile selection.
func permissionTargets(c *call, profile string, all bool) ([]profiles.PermissionTarget, error) {
	if _, err := c.appConfig(); err != nil {
		return nil, err
	}
	store, err := c.profileStore()
	if err != nil {
		return nil, err
	}
	return store.PermissionTargets(profile, all)
}

func permissionAdd(c *call, profile string, all bool, rule string) error {
	targets, err := permissionTargets(c, profile, all)
	if err != nil {
		return err
	}
	changes, err := profiles.AddAllowRule(c.fx, targets, rule)
	verb := "added"
	if c.previewing() {
		verb = "would add"
	}
	for _, change := range changes {
		if change.Changed {
			c.sayf("%s: %s %s to allow", change.Profile, verb, rule)
		} else {
			c.sayf("%s: already in allow", change.Profile)
		}
	}
	return err
}

func permissionRemove(c *call, profile string, all bool, rule string) error {
	targets, err := permissionTargets(c, profile, all)
	if err != nil {
		return err
	}
	changes, err := profiles.RemoveAllowRule(c.fx, targets, rule)
	verb := "removed"
	if c.previewing() {
		verb = "would remove"
	}
	for _, change := range changes {
		if change.Changed {
			c.sayf("%s: %s %s from allow", change.Profile, verb, rule)
		} else {
			c.sayf("%s: not found in allow", change.Profile)
		}
	}
	return err
}

func permissionList(c *call, profile string, all bool, format, category string) error {
	targets, err := permissionTargets(c, profile, all)
	if err != nil {
		return err
	}
	listed, err := profiles.ListRules(targets, category)
	if err != nil {
		return err
	}
	multi := len(listed) > 1
	answer := make([]interface{}, 0, len(listed))
	for i, p := range listed {
		if multi {
			if i > 0 {
				c.say("")
			}
			c.sayf("[%s]", p.Profile)
		}
		subset := map[string]interface{}{}
		for _, cr := range p.Categories {
			rules := make([]interface{}, len(cr.Rules))
			for j, r := range cr.Rules {
				rules[j] = r
			}
			subset[cr.Category] = rules
			switch format {
			case formatGrouped:
				c.sayf("  %s:", cr.Category)
				if len(cr.Rules) == 0 {
					c.say("    (none)")
				}
				for _, r := range cr.Rules {
					c.say("    " + r)
				}
			case formatFlat:
				for _, r := range cr.Rules {
					c.say(cr.Category + "\t" + r)
				}
			}
		}
		answer = append(answer, map[string]interface{}{"name": p.Profile, "permissions": subset})
	}
	c.ctx.Payload(map[string]interface{}{"profiles": answer})
	return nil
}
