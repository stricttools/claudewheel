package sessionmove

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/stricttools/claudewheel/internal/effects"
	"github.com/stricttools/claudewheel/internal/lifecycle"
	"github.com/stricttools/claudewheel/internal/sessions"
	"github.com/stricttools/claudewheel/internal/workspace"
)

// importSimpleDirs are the source directories whose entries belong to one
// session by name.
var importSimpleDirs = []string{"todos", "session-env", "file-history", "tasks"}

// PathMapping maps a project path recorded in the source (any spelling: a
// Windows path with either separator and either drive-letter case, or a
// POSIX path) to the absolute local directory it becomes.
type PathMapping struct {
	From string
	To   string
}

// ImportOptions are the choices of one Import run.
type ImportOptions struct {
	// Reid gives a session that collides with one in the shared store a new
	// uuid; without it any collision stops the import before it copies.
	Reid bool
	// Warnings receives the lines about transcripts skipped because they
	// disappeared during the scan (the CLI passes stderr). Required.
	Warnings io.Writer
}

// ImportResult counts what an import did, or would do. Collisions lists the
// colliding sessions when Reid was not set; nothing was copied then.
type ImportResult struct {
	SessionsImported int
	SessionsReided   int
	ArtifactsCopied  int
	LinesRewritten   int
	PasteFilesCopied int
	Collisions       []string
}

// importer is one Import run.
type importer struct {
	fx     *effects.FX
	store  workspace.SharedStore
	warn   io.Writer
	result ImportResult
}

func (im *importer) log(msg string) {
	im.fx.Info("[import] " + msg)
}

func (im *importer) would(would, did string) string {
	if im.fx.Previewing() {
		return would
	}
	return did
}

// brokenLinks returns a line for every symbolic link in the source whose
// target does not exist, among the entries the import reads: every
// transcript; for each session it imports, its folder and every entry under
// it, its todos files, its session-env, file-history, and tasks entries (with
// the trees under them, whose links are followed when copied); and every
// paste-cache entry. A store archived on another machine carries that
// machine's absolute links. A transcript that is an empty file is not
// imported, so its session's entries are not checked.
func brokenLinks(source string) ([]string, error) {
	var broken []string
	check := func(path string) (bool, error) {
		bad, err := isDanglingLink(path)
		if err != nil || !bad {
			return false, err
		}
		target, err := os.Readlink(path)
		if err != nil {
			return false, err
		}
		broken = append(broken, fmt.Sprintf("  %s: dangling symlink (its target %s does not exist)", path, target))
		return true, nil
	}

	encodedDirs, err := subdirs(filepath.Join(source, workspace.ProjectsDirName))
	if err != nil {
		return nil, err
	}
	imported := map[string]bool{}
	for _, dir := range encodedDirs {
		names, err := dirNames(dir)
		if err != nil {
			return nil, err
		}
		for _, name := range names {
			stem, ok := strings.CutSuffix(name, ".jsonl")
			if !ok {
				continue
			}
			entry := filepath.Join(dir, name)
			bad, err := check(entry)
			if err != nil {
				return nil, err
			}
			if !isUUID(stem) {
				continue
			}
			if !bad {
				info, err := os.Stat(entry)
				if errors.Is(err, os.ErrNotExist) {
					continue
				}
				if err != nil {
					return nil, err
				}
				if info.Size() == 0 {
					continue
				}
			}
			imported[stem] = true
			companion := filepath.Join(dir, stem)
			bad, err = check(companion)
			if err != nil {
				return nil, err
			}
			if bad {
				continue
			}
			folder, err := isDir(companion)
			if err != nil {
				return nil, err
			}
			if !folder {
				continue
			}
			items, err := walkMatching(companion, func(string) bool { return true })
			if err != nil {
				return nil, err
			}
			for _, item := range items {
				if _, err := check(item); err != nil {
					return nil, err
				}
			}
		}
	}

	uuids := make([]string, 0, len(imported))
	for u := range imported {
		uuids = append(uuids, u)
	}
	sort.Strings(uuids)
	todos, err := namesIfDir(filepath.Join(source, "todos"))
	if err != nil {
		return nil, err
	}
	for _, uuid := range uuids {
		for _, name := range todos {
			if strings.HasPrefix(name, uuid+"-agent-") && strings.HasSuffix(name, ".json") {
				if _, err := check(filepath.Join(source, "todos", name)); err != nil {
					return nil, err
				}
			}
		}
		for _, d := range importSimpleDirs {
			if d == "todos" {
				continue
			}
			artifact := filepath.Join(source, d, uuid)
			bad, err := check(artifact)
			if err != nil {
				return nil, err
			}
			if bad {
				continue
			}
			dir, err := isDir(artifact)
			if err != nil {
				return nil, err
			}
			if dir {
				if err := checkTree(artifact, check, map[string]bool{}); err != nil {
					return nil, err
				}
			}
		}
	}

	pastes, err := namesIfDir(filepath.Join(source, "paste-cache"))
	if err != nil {
		return nil, err
	}
	for _, name := range pastes {
		if _, err := check(filepath.Join(source, "paste-cache", name)); err != nil {
			return nil, err
		}
	}
	return broken, nil
}

// checkTree runs check on every entry under the directory root, following
// symbolic links to directories as a tree copy does; a directory already
// seen (by its resolved path) is not walked again.
func checkTree(root string, check func(string) (bool, error), seen map[string]bool) error {
	resolved, err := filepath.EvalSymlinks(root)
	if err != nil {
		return err
	}
	if seen[resolved] {
		return nil
	}
	seen[resolved] = true
	names, err := dirNames(root)
	if err != nil {
		return err
	}
	for _, name := range names {
		path := filepath.Join(root, name)
		bad, err := check(path)
		if err != nil {
			return err
		}
		if bad {
			continue
		}
		dir, err := isDir(path)
		if err != nil {
			return err
		}
		if dir {
			if err := checkTree(path, check, seen); err != nil {
				return err
			}
		}
	}
	return nil
}

// normalizeCwd folds a cwd for comparison: trailing '/' and '\' stripped,
// and a drive letter lowercased.
func normalizeCwd(cwd string) string {
	s := strings.TrimRight(cwd, `/\`)
	first, size := utf8.DecodeRuneInString(s)
	second, _ := utf8.DecodeRuneInString(s[size:])
	if size > 0 && second == ':' && unicode.IsLetter(first) {
		return string(unicode.ToLower(first)) + s[size:]
	}
	return s
}

// rewriter replaces one spelling of a mapped path, and any deeper segments
// after it, with the local path. Its prefix is the mapping's path
// components joined by sep, behind a "/" when lead is set; with drive set
// the first character (a drive letter) matches in either case.
type rewriter struct {
	sep    string
	lead   bool
	drive  bool
	prefix string
	to     string
}

// buildRewriters returns two rewriters per mapping, the longest source path
// first so a longer prefix is replaced before a shorter one it starts with:
// one for the JSON-escaped backslash spelling (c:\\Users\\m, as a Windows
// path appears inside a JSON string) and one for the forward-slash spelling.
func buildRewriters(mappings []PathMapping) ([]rewriter, error) {
	sorted := append([]PathMapping(nil), mappings...)
	sort.SliceStable(sorted, func(i, j int) bool {
		return utf8.RuneCountInString(sorted[i].From) > utf8.RuneCountInString(sorted[j].From)
	})
	var out []rewriter
	for _, m := range sorted {
		parts := strings.FieldsFunc(m.From, func(r rune) bool { return r == '/' || r == '\\' })
		if len(parts) == 0 {
			return nil, fmt.Errorf("the mapped source path %s names no directory", quoted(m.From))
		}
		// A drive letter is a first component of two characters, the second
		// a colon; the first component is empty for a path starting with a
		// separator.
		head, _, _ := strings.Cut(strings.ReplaceAll(m.From, `\`, "/"), "/")
		drive := utf8.RuneCountInString(head) == 2 && strings.HasSuffix(head, ":")
		out = append(out,
			rewriter{sep: `\\`, drive: drive, prefix: strings.Join(parts, `\\`), to: m.To},
			rewriter{sep: "/", lead: strings.HasPrefix(m.From, "/"), drive: drive, prefix: strings.Join(parts, "/"), to: m.To},
		)
	}
	return out, nil
}

// matchPrefix returns the end of the rewriter's prefix when it starts at
// line[i].
func (r rewriter) matchPrefix(line string, i int) (int, bool) {
	rest := line[i:]
	if r.lead {
		if !strings.HasPrefix(rest, "/") {
			return 0, false
		}
		rest = rest[1:]
	}
	want := r.prefix
	if r.drive {
		letter, size := utf8.DecodeRuneInString(want)
		got, gotSize := utf8.DecodeRuneInString(rest)
		if gotSize == 0 || (got != unicode.ToLower(letter) && got != unicode.ToUpper(letter)) {
			return 0, false
		}
		want, rest = want[size:], rest[gotSize:]
	}
	if !strings.HasPrefix(rest, want) {
		return 0, false
	}
	return len(line) - len(rest) + len(want), true
}

func isHex(c byte) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}

// suffixEnd returns the end of the deeper segments after a matched prefix:
// each is the separator and one or more characters other than '\', '"',
// '[', ']', a space, and (in the forward-slash spelling) '/'. A backslash
// segment never starts with a \uXXXX escape. ANSI escapes and prose stop the
// path.
func (r rewriter) suffixEnd(line string, k int) int {
	stops := `\"[] `
	if r.sep == "/" {
		stops += "/"
	}
	for strings.HasPrefix(line[k:], r.sep) {
		next := k + len(r.sep)
		if next >= len(line) || strings.IndexByte(stops, line[next]) >= 0 {
			break
		}
		if r.sep == `\\` && line[next] == 'u' && next+4 < len(line) &&
			isHex(line[next+1]) && isHex(line[next+2]) && isHex(line[next+3]) && isHex(line[next+4]) {
			break
		}
		k = next
		for k < len(line) && strings.IndexByte(stops, line[k]) < 0 {
			k++
		}
	}
	return k
}

// apply replaces every non-overlapping occurrence, leftmost first, with the
// local path followed by the deeper segments in forward-slash form.
func (r rewriter) apply(line string) (string, bool) {
	var b strings.Builder
	copied, changed := 0, false
	for i := 0; i < len(line); {
		end, ok := r.matchPrefix(line, i)
		if !ok {
			_, size := utf8.DecodeRuneInString(line[i:])
			i += size
			continue
		}
		k := r.suffixEnd(line, end)
		suffix := strings.ReplaceAll(strings.ReplaceAll(line[end:k], `\\`, "/"), `\`, "/")
		b.WriteString(line[copied:i])
		b.WriteString(r.to)
		b.WriteString(suffix)
		copied, i, changed = k, k, true
	}
	if !changed {
		return line, false
	}
	b.WriteString(line[copied:])
	return b.String(), true
}

// rewriteJSONL copies a transcript from src to dst with every rewriter
// applied to each line, and, when newUUID is set, every
// "sessionId":"<oldUUID>" changed to the new uuid. It returns the number of
// lines changed.
func (im *importer) rewriteJSONL(src, dst string, rewriters []rewriter, oldUUID, newUUID string) (int, error) {
	data, err := readTranscript(src)
	if err != nil {
		return 0, err
	}
	lines := bytes.SplitAfter(data, []byte("\n"))
	count := 0
	var out bytes.Buffer
	for _, raw := range lines {
		line, changed := string(raw), false
		for _, r := range rewriters {
			if next, ok := r.apply(line); ok {
				line, changed = next, true
			}
		}
		if newUUID != "" && oldUUID != newUUID {
			next := strings.ReplaceAll(line, `"sessionId":"`+oldUUID+`"`, `"sessionId":"`+newUUID+`"`)
			if next == line {
				next = strings.ReplaceAll(line, `"sessionId": "`+oldUUID+`"`, `"sessionId": "`+newUUID+`"`)
			}
			if next != line {
				line, changed = next, true
			}
		}
		if changed {
			count++
		}
		out.WriteString(line)
	}
	if err := im.fx.MkdirAll(filepath.Dir(dst)); err != nil {
		return 0, err
	}
	if err := im.fx.WriteFileAtomic(dst, out.Bytes()); err != nil {
		return 0, err
	}
	return count, nil
}

// sessionBundle is a session's transcript plus its optional folder.
type sessionBundle struct {
	uuid      string
	jsonlPath string
	// companionDir is the <uuid>/ folder beside the transcript, "" for none.
	companionDir string
	// cwd is the path its store dir's sessions recorded (StoreDirPath).
	cwd string
}

// scanSource collects the session bundles under <source>/projects/*/, which
// brokenLinks found free of dangling symbolic links. A store dir whose
// sessions do not record one matching path is an error, found before the
// import writes anything.
func (im *importer) scanSource(source string) ([]sessionBundle, error) {
	encodedDirs, err := subdirs(filepath.Join(source, workspace.ProjectsDirName))
	if err != nil {
		return nil, err
	}

	var bundles []sessionBundle
	for _, dir := range encodedDirs {
		names, err := dirNames(dir)
		if err != nil {
			return nil, err
		}
		cwd := ""
		for _, name := range names {
			stem, ok := strings.CutSuffix(name, ".jsonl")
			if !ok || !isUUID(stem) {
				continue
			}
			entry := filepath.Join(dir, name)
			info, err := os.Stat(entry)
			if errors.Is(err, os.ErrNotExist) {
				fmt.Fprintf(im.warn, "[import] WARNING: skipping %s: it disappeared during the scan\n", entry)
				continue
			}
			if err != nil {
				return nil, err
			}
			if info.Size() == 0 {
				continue
			}
			if cwd == "" {
				if cwd, err = sessions.StoreDirPath(dir); err != nil {
					return nil, err
				}
			}
			companion := filepath.Join(dir, stem)
			folder, err := isDir(companion)
			if err != nil {
				return nil, err
			}
			b := sessionBundle{uuid: stem, jsonlPath: entry, cwd: cwd}
			if folder {
				b.companionDir = companion
			}
			bundles = append(bundles, b)
		}
	}
	return bundles, nil
}

func isDanglingLink(path string) (bool, error) {
	link, err := isSymlink(path)
	if err != nil || !link {
		return false, err
	}
	there, err := exists(path)
	return !there, err
}

// newSessionUUID returns a random uuid in Claude Code's 8-4-4-4-12 spelling.
func newSessionUUID() string {
	h := lifecycle.NewUUID4Hex()
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

// bundleTarget is where one bundle goes: its store dir, and its new uuid when it
// was given one.
type bundleTarget struct {
	dir     string
	newUUID string
}

// Import copies the sessions of an external Claude Code directory (one
// holding projects/) into the shared store, rewriting the mapped paths in
// every transcript. Every cwd the source's sessions recorded needs a
// mapping. A session already in the shared store is a collision: without
// Reid the import copies nothing and returns the collisions in the result;
// with it the session gets a new uuid. A symbolic link the import would read
// whose target does not exist is an error listing every such link (see
// brokenLinks), and nothing is copied.
func Import(fx *effects.FX, store workspace.SharedStore, source string, mappings []PathMapping, opts ImportOptions) (ImportResult, error) {
	if opts.Warnings == nil {
		return ImportResult{}, errors.New("import needs a writer for its warnings")
	}
	im := &importer{fx: fx, store: store, warn: opts.Warnings}

	ok, err := isDir(filepath.Join(source, workspace.ProjectsDirName))
	if err != nil {
		return im.result, err
	}
	if !ok {
		return im.result, fmt.Errorf("source directory does not contain a projects/ subdirectory: %s", source)
	}
	if fx.Previewing() {
		im.log("DRY RUN -- no changes will be made")
	}

	im.log("scanning " + source)
	broken, err := brokenLinks(source)
	if err != nil {
		return im.result, err
	}
	if len(broken) > 0 {
		return im.result, fmt.Errorf("cannot import, nothing was changed: these symlinks in the source have no target:\n%s", strings.Join(broken, "\n"))
	}
	bundles, err := im.scanSource(source)
	if err != nil {
		return im.result, err
	}
	im.log(fmt.Sprintf("found %d session bundles", len(bundles)))
	if len(bundles) == 0 {
		im.log("nothing to import")
		return im.result, nil
	}

	// Every discovered cwd needs a mapping.
	toPath := map[string]string{}
	for _, m := range mappings {
		toPath[normalizeCwd(m.From)] = m.To
	}
	firstCwd := map[string]string{}
	for _, b := range bundles {
		key := normalizeCwd(b.cwd)
		if _, mapped := toPath[key]; mapped {
			continue
		}
		if _, listed := firstCwd[key]; !listed {
			firstCwd[key] = b.cwd
		}
	}
	if len(firstCwd) > 0 {
		keys := make([]string, 0, len(firstCwd))
		for k := range firstCwd {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		lines := make([]string, len(keys))
		for i, k := range keys {
			lines[i] = "  " + firstCwd[k]
		}
		return im.result, fmt.Errorf("unmapped cwds found in source -- add a --from and --to pair for each:\n%s", strings.Join(lines, "\n"))
	}

	// Compute every target and detect collisions before copying anything.
	targets := make([]bundleTarget, len(bundles))
	for i, b := range bundles {
		dir := filepath.Join(store.ProjectsDir(), workspace.EncodePath(toPath[normalizeCwd(b.cwd)]))
		jsonlTaken, err := exists(filepath.Join(dir, b.uuid+".jsonl"))
		if err != nil {
			return im.result, err
		}
		folderTaken, err := exists(filepath.Join(dir, b.uuid))
		if err != nil {
			return im.result, err
		}
		targets[i] = bundleTarget{dir: dir}
		if !jsonlTaken && !folderTaken {
			continue
		}
		if opts.Reid {
			targets[i].newUUID = newSessionUUID()
		} else {
			im.result.Collisions = append(im.result.Collisions, fmt.Sprintf("%s -> %s (from %s)", b.uuid, dir, b.cwd))
		}
	}
	if len(im.result.Collisions) > 0 {
		im.log(fmt.Sprintf("%d collision(s) detected, --reid not set", len(im.result.Collisions)))
		for _, c := range im.result.Collisions {
			im.log("  COLLISION: " + c)
		}
		return im.result, nil
	}

	rewriters, err := buildRewriters(mappings)
	if err != nil {
		return im.result, err
	}
	for i, b := range bundles {
		if err := im.copyBundle(source, b, targets[i], rewriters); err != nil {
			return im.result, err
		}
	}
	if err := im.copyPasteCache(source); err != nil {
		return im.result, err
	}

	im.log("summary")
	im.log(fmt.Sprintf("  sessions imported:   %d", im.result.SessionsImported))
	im.log(fmt.Sprintf("  sessions re-IDed:    %d", im.result.SessionsReided))
	im.log(fmt.Sprintf("  artifacts copied:    %d", im.result.ArtifactsCopied))
	im.log(fmt.Sprintf("  lines rewritten:     %d", im.result.LinesRewritten))
	im.log(fmt.Sprintf("  paste files copied:  %d", im.result.PasteFilesCopied))
	if fx.Previewing() {
		im.log("  (dry run -- nothing written)")
	}
	return im.result, nil
}

// copyBundle copies one session: its rewritten transcript, its folder (with
// nested transcripts rewritten), and its artifacts in the simple directories.
func (im *importer) copyBundle(source string, b sessionBundle, t bundleTarget, rewriters []rewriter) error {
	reided := t.newUUID != ""
	effective := b.uuid
	if reided {
		effective = t.newUUID
		im.result.SessionsReided++
	}
	targetJSONL := filepath.Join(t.dir, effective+".jsonl")
	im.log(fmt.Sprintf("  %s %s -> %s", im.would("would copy", "copying"), b.jsonlPath, targetJSONL))
	lines, err := im.rewriteJSONL(b.jsonlPath, targetJSONL, rewriters, b.uuid, t.newUUID)
	if err != nil {
		return err
	}
	im.result.LinesRewritten += lines
	im.result.SessionsImported++

	if b.companionDir != "" {
		targetCompanion := filepath.Join(t.dir, effective)
		if err := im.fx.MkdirAll(targetCompanion); err != nil {
			return err
		}
		items, err := walkMatching(b.companionDir, func(string) bool { return true })
		if err != nil {
			return err
		}
		for _, item := range items {
			file, err := isFile(item)
			if err != nil {
				return err
			}
			if !file {
				continue
			}
			rel := strings.TrimPrefix(item, strings.TrimSuffix(b.companionDir, "/")+"/")
			dstRel := rel
			if reided {
				dstRel = strings.ReplaceAll(rel, b.uuid, effective)
			}
			dst := filepath.Join(targetCompanion, dstRel)
			name := filepath.Base(item)
			if strings.HasSuffix(name, ".jsonl") && len(name) > len(".jsonl") {
				im.log(fmt.Sprintf("    %s %s", im.would("would rewrite", "rewriting"), rel))
				lines, err := im.rewriteJSONL(item, dst, rewriters, b.uuid, t.newUUID)
				if err != nil {
					return err
				}
				im.result.LinesRewritten += lines
			} else {
				if err := im.fx.MkdirAll(filepath.Dir(dst)); err != nil {
					return err
				}
				if err := im.fx.CopyFile(item, dst); err != nil {
					return err
				}
				im.log(fmt.Sprintf("    %s %s", im.would("would copy", "copied"), rel))
			}
			im.result.ArtifactsCopied++
		}
	}

	for _, d := range importSimpleDirs {
		if err := im.copySimpleArtifacts(source, d, b.uuid, effective); err != nil {
			return err
		}
	}
	return nil
}

// copySimpleArtifacts copies one session's entries of a simple directory:
// todos/<uuid>-agent-*.json files, or the <uuid> entry of session-env,
// file-history, and tasks. An entry already in the shared store is left as
// it is.
func (im *importer) copySimpleArtifacts(source, dirname, oldUUID, effective string) error {
	srcDir := filepath.Join(source, dirname)
	ok, err := isDir(srcDir)
	if err != nil || !ok {
		return err
	}
	dstBase := im.store.Subdir(dirname)

	if dirname == "todos" {
		names, err := dirNames(srcDir)
		if err != nil {
			return err
		}
		for _, name := range names {
			if !strings.HasPrefix(name, oldUUID+"-agent-") || !strings.HasSuffix(name, ".json") {
				continue
			}
			item := filepath.Join(srcDir, name)
			file, err := isFile(item)
			if err != nil {
				return err
			}
			if !file {
				continue
			}
			newName := name
			if effective != oldUUID {
				newName = strings.ReplaceAll(name, oldUUID, effective)
			}
			dst := filepath.Join(dstBase, newName)
			taken, err := exists(dst)
			if err != nil {
				return err
			}
			if taken {
				continue
			}
			if err := im.fx.MkdirAll(dstBase); err != nil {
				return err
			}
			if err := im.fx.CopyFile(item, dst); err != nil {
				return err
			}
			im.log(fmt.Sprintf("  %s %s/%s", im.would("would copy", "copied"), dirname, newName))
			im.result.ArtifactsCopied++
		}
		return nil
	}

	artifact := filepath.Join(srcDir, oldUUID)
	there, err := exists(artifact)
	if err != nil || !there {
		return err
	}
	dst := filepath.Join(dstBase, effective)
	taken, err := exists(dst)
	if err != nil || taken {
		return err
	}
	if err := im.fx.MkdirAll(filepath.Dir(dst)); err != nil {
		return err
	}
	dir, err := isDir(artifact)
	if err != nil {
		return err
	}
	if dir {
		err = im.fx.CopyTree(artifact, dst)
	} else {
		err = im.fx.CopyFile(artifact, dst)
	}
	if err != nil {
		return err
	}
	im.log(fmt.Sprintf("  %s %s/%s", im.would("would copy", "copied"), dirname, effective))
	im.result.ArtifactsCopied++
	return nil
}

// copyPasteCache copies the source's paste-cache files the shared store
// does not hold yet; they are named by content hash, so a name already
// there is the same file.
func (im *importer) copyPasteCache(source string) error {
	pasteSrc := filepath.Join(source, "paste-cache")
	ok, err := isDir(pasteSrc)
	if err != nil || !ok {
		return err
	}
	pasteDst := im.store.Subdir("paste-cache")
	if err := im.fx.MkdirAll(pasteDst); err != nil {
		return err
	}
	names, err := dirNames(pasteSrc)
	if err != nil {
		return err
	}
	for _, name := range names {
		item := filepath.Join(pasteSrc, name)
		file, err := isFile(item)
		if err != nil {
			return err
		}
		if !file {
			continue
		}
		dst := filepath.Join(pasteDst, name)
		taken, err := exists(dst)
		if err != nil {
			return err
		}
		if taken {
			continue
		}
		if err := im.fx.CopyFile(item, dst); err != nil {
			return err
		}
		im.log(fmt.Sprintf("  %s paste-cache/%s", im.would("would copy", "copied"), name))
		im.result.PasteFilesCopied++
	}
	return nil
}
