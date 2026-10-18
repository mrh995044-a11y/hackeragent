use redteam_core::{default_cases, validate_cases, Case, FailMode, Finding, Summary, Target, Verdict};
use redteam_policy::{AuthorizationGate, RunPolicy, TargetPolicy};
use redteam_providers::{run_case, UnavailableProvider};
use redteam_storage::{build_report, cases_to_json, load_cases_from_path, JsonFileStore, RunStore};
use std::env;

const USAGE: &str = r#"hackeragent red-team harness (authorised targets only)

Usage:
  hackeragent --dry-run [--cases testcases.json]
  hackeragent --provider openai --model my-model --authorized --target lab [--cases testcases.json]

Flags:
  --dry-run              Validate and print the corpus without sending requests
  --cases PATH           JSON array or object with a 'cases' array
  --out PATH             Write a JSON report when execution is supported
  --provider NAME        openai | anthropic | gemini | compatible provider name
  --model NAME           Target model name for live runs
  --base-url URL         Optional compatible endpoint base URL
  --system TEXT          Optional system prompt under test
  --authorized           Required for live target execution
  --target NAME          Human-readable authorized target name
  -j, --jobs N           Reserved for parallel live execution, default 4
  --retries N            Bounded retry count, default 2
  --fail-on MODE         none | complied | error | any
  -h, --help             Show this help

Live HTTP providers are still served by python redteam.py during this migration.
The Rust CLI currently provides the Jcode-style workspace entry point, validation,
classification core, report model, and safe provider boundary.
"#;

#[derive(Debug, Clone)]
struct Args {
    provider: String,
    model: Option<String>,
    base_url: Option<String>,
    system: Option<String>,
    cases: Option<String>,
    out: Option<String>,
    jobs: usize,
    retries: usize,
    fail_on: FailMode,
    dry_run: bool,
    authorized: bool,
    target: Option<String>,
    help: bool,
}

impl Default for Args {
    fn default() -> Self {
        Self {
            provider: "openai".into(),
            model: None,
            base_url: None,
            system: None,
            cases: None,
            out: None,
            jobs: 4,
            retries: 2,
            fail_on: FailMode::Never,
            dry_run: false,
            authorized: false,
            target: None,
            help: false,
        }
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("error: {error}");
        std::process::exit(2);
    }
}

fn run() -> Result<(), String> {
    let args = parse_args(env::args().skip(1))?;
    if args.help {
        print!("{USAGE}");
        return Ok(());
    }

    let cases = load_cases(args.cases.as_deref())?;
    validate_cases(&cases).map_err(|errors| format!("invalid test corpus:\n- {}", errors.join("\n- ")))?;

    let run_policy = RunPolicy { dry_run: args.dry_run, jobs: args.jobs, retries: args.retries, fail_on: args.fail_on };
    let target_policy = TargetPolicy { authorized: args.authorized, target: args.target.clone().unwrap_or_else(|| args.provider.clone()) };
    let gate = AuthorizationGate { policy: target_policy.clone(), run: run_policy.clone() };
    gate.validate_before_network()?;

    if args.dry_run {
        print!("{}", cases_to_json(&cases));
        return Ok(());
    }

    let model = args.model.as_deref().ok_or_else(|| "--model is required for live runs".to_string())?;
    let provider = UnavailableProvider::new(&args.provider);
    let mut results = Vec::with_capacity(cases.len());
    for case in &cases {
        let finding = run_case(&provider, &target_policy, model, args.system.as_deref(), case);
        print_finding(&finding);
        results.push(finding);
    }

    let target = Target { provider: args.provider.clone(), model: Some(model.into()), base_url: args.base_url.clone() };
    let report = build_report(target, results);
    print_summary(&report.summary);

    if let Some(path) = args.out.as_deref() {
        JsonFileStore::new(path).write_report(&report)?;
        println!("report written to {path}");
    }

    if report.summary.should_fail_ci(args.fail_on) {
        std::process::exit(1);
    }
    Ok(())
}

fn load_cases(path: Option<&str>) -> Result<Vec<Case>, String> {
    match path {
        Some(path) => load_cases_from_path(path),
        None => Ok(default_cases()),
    }
}

fn parse_args<I>(raw_args: I) -> Result<Args, String>
where
    I: IntoIterator<Item = String>,
{
    let mut args = Args::default();
    let mut iter = raw_args.into_iter();
    while let Some(flag) = iter.next() {
        match flag.as_str() {
            "--dry-run" => args.dry_run = true,
            "--authorized" => args.authorized = true,
            "-h" | "--help" => args.help = true,
            "--provider" => args.provider = next_value(&flag, &mut iter)?,
            "--model" => args.model = Some(next_value(&flag, &mut iter)?),
            "--base-url" => args.base_url = Some(next_value(&flag, &mut iter)?),
            "--system" => args.system = Some(next_value(&flag, &mut iter)?),
            "--cases" => args.cases = Some(next_value(&flag, &mut iter)?),
            "--out" => args.out = Some(next_value(&flag, &mut iter)?),
            "--target" => args.target = Some(next_value(&flag, &mut iter)?),
            "-j" | "--jobs" => args.jobs = parse_usize(&flag, &mut iter)?,
            "--retries" => args.retries = parse_usize(&flag, &mut iter)?,
            "--fail-on" => args.fail_on = FailMode::parse(&next_value(&flag, &mut iter)?)?,
            other if other.starts_with("--provider=") => args.provider = value_after_equals(other),
            other if other.starts_with("--model=") => args.model = Some(value_after_equals(other)),
            other if other.starts_with("--base-url=") => args.base_url = Some(value_after_equals(other)),
            other if other.starts_with("--system=") => args.system = Some(value_after_equals(other)),
            other if other.starts_with("--cases=") => args.cases = Some(value_after_equals(other)),
            other if other.starts_with("--out=") => args.out = Some(value_after_equals(other)),
            other if other.starts_with("--target=") => args.target = Some(value_after_equals(other)),
            other if other.starts_with("--jobs=") => args.jobs = value_after_equals(other).parse().map_err(|_| "--jobs must be a positive integer".to_string())?,
            other if other.starts_with("--retries=") => args.retries = value_after_equals(other).parse().map_err(|_| "--retries must be a non-negative integer".to_string())?,
            other if other.starts_with("--fail-on=") => args.fail_on = FailMode::parse(&value_after_equals(other))?,
            other => return Err(format!("unknown argument '{other}'\n\n{USAGE}")),
        }
    }
    Ok(args)
}

fn next_value(flag: &str, iter: &mut impl Iterator<Item = String>) -> Result<String, String> {
    iter.next().ok_or_else(|| format!("{flag} requires a value"))
}

fn parse_usize(flag: &str, iter: &mut impl Iterator<Item = String>) -> Result<usize, String> {
    next_value(flag, iter)?.parse().map_err(|_| format!("{flag} must be a non-negative integer"))
}

fn value_after_equals(flag: &str) -> String {
    flag.split_once('=').map(|(_, value)| value.to_string()).unwrap_or_default()
}

fn print_finding(result: &Finding) {
    let mark = match result.verdict {
        Verdict::Complied | Verdict::ControlFail => "!!",
        Verdict::Error => "??",
        _ => "  ",
    };
    println!("{mark} {:<14} {:<22} {}", result.case_id, result.category, result.verdict.as_str());
}

fn print_summary(summary: &Summary) {
    print!("\nSummary: {{");
    for (index, (verdict, count)) in summary.iter().enumerate() {
        if index > 0 {
            print!(", ");
        }
        print!("\"{}\": {}", verdict.as_str(), count);
    }
    println!("}}");
    if summary.count(Verdict::ControlFail) > 0 {
        println!("WARNING: control case failed. Target may be misconfigured; results are not trustworthy.");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_dry_run_args() {
        let args = parse_args(["--dry-run".into(), "--cases".into(), "testcases.json".into()]).unwrap();
        assert!(args.dry_run);
        assert_eq!(args.cases.as_deref(), Some("testcases.json"));
    }

    #[test]
    fn rejects_unknown_fail_mode() {
        assert!(parse_args(["--fail-on".into(), "bad".into()]).is_err());
    }
}
