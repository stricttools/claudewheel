package widgets

// AuthOutcome is how an authentication flow ended: the create wizard's, and
// the one the bar offers before launching an unauthenticated profile.
type AuthOutcome string

const (
	// AuthAuthenticated: the login completed, or a token was validated
	// against the API and saved.
	AuthAuthenticated AuthOutcome = "authenticated"
	// AuthUnverified: the API could not be asked and the user chose to save
	// the token without validation.
	AuthUnverified AuthOutcome = "unverified"
	// AuthSkipped: the user chose to skip (before a launch: to launch
	// without authenticating).
	AuthSkipped AuthOutcome = "skip"
	// AuthCancelled: the user cancelled a choice with Escape; before a
	// launch, nothing launches.
	AuthCancelled AuthOutcome = "cancel"
	// AuthFailed: authentication was attempted and did not complete;
	// credentials may be partly written.
	AuthFailed AuthOutcome = "failed"
)
