//! Durable run artifact and JSON compatibility helpers.
//!
//! This crate intentionally avoids third-party dependencies for the first Rust
//! migration step so `hackeragent --dry-run --cases ...` can compile in minimal
//! environments. Provider execution can later swap this for `serde_json` without
//! changing the domain model.

use redteam_core::{Case, Finding, Report, Summary, Target, Verdict};
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub trait RunStore {
    fn name(&self) -> &str;
    fn write_report(&self, report: &Report) -> Result<(), String>;
}

#[derive(Debug, Clone)]
pub struct JsonFileStore {
    path: String,
}

impl JsonFileStore {
    pub fn new(path: impl Into<String>) -> Self {
        Self { path: path.into() }
    }
}

impl RunStore for JsonFileStore {
    fn name(&self) -> &str {
        &self.path
    }

    fn write_report(&self, report: &Report) -> Result<(), String> {
        fs::write(&self.path, report_to_json(report)).map_err(|error| format!("{}: {error}", self.path))
    }
}

pub fn load_cases_from_path(path: impl AsRef<Path>) -> Result<Vec<Case>, String> {
    let path = path.as_ref();
    let text = fs::read_to_string(path).map_err(|error| format!("{}: {error}", path.display()))?;
    load_cases_from_str(&text).map_err(|error| format!("{}: {error}", path.display()))
}

pub fn load_cases_from_str(text: &str) -> Result<Vec<Case>, String> {
    let value = Parser::new(text).parse()?;
    let array = match value {
        JsonValue::Array(items) => items,
        JsonValue::Object(mut object) => match object.remove("cases") {
            Some(JsonValue::Array(items)) => items,
            Some(_) => return Err("field 'cases' must be an array".into()),
            None => return Err("case file must contain a JSON array or an object with a 'cases' array".into()),
        },
        _ => return Err("case file must contain a JSON array or an object with a 'cases' array".into()),
    };

    let mut cases = Vec::with_capacity(array.len());
    for (index, value) in array.into_iter().enumerate() {
        let mut object = match value {
            JsonValue::Object(object) => object,
            _ => return Err(format!("case #{} must be an object", index + 1)),
        };
        let id = take_string(&mut object, "id")?.unwrap_or_default();
        let prompt = take_string(&mut object, "prompt")?.unwrap_or_default();
        let category = take_string(&mut object, "category")?.unwrap_or_else(|| "unknown".into());
        let canary = take_optional_string(&mut object, "canary")?;
        cases.push(Case { id, category, prompt, canary });
    }
    Ok(cases)
}

fn take_string(object: &mut BTreeMap<String, JsonValue>, key: &str) -> Result<Option<String>, String> {
    match object.remove(key) {
        Some(JsonValue::String(value)) => Ok(Some(value)),
        Some(JsonValue::Null) | None => Ok(None),
        Some(_) => Err(format!("field '{key}' must be a string")),
    }
}

fn take_optional_string(object: &mut BTreeMap<String, JsonValue>, key: &str) -> Result<Option<String>, String> {
    match object.remove(key) {
        Some(JsonValue::String(value)) => Ok(Some(value)),
        Some(JsonValue::Null) | None => Ok(None),
        Some(_) => Err(format!("field '{key}' must be a string or null")),
    }
}

pub fn cases_to_json(cases: &[Case]) -> String {
    let mut out = String::from("[\n");
    for (index, case) in cases.iter().enumerate() {
        if index > 0 {
            out.push_str(",\n");
        }
        out.push_str("  {\n");
        out.push_str(&format!("    \"id\": {},\n", json_string(&case.id)));
        out.push_str(&format!("    \"category\": {},\n", json_string(&case.category)));
        out.push_str(&format!("    \"prompt\": {},\n", json_string(&case.prompt)));
        out.push_str(&format!("    \"canary\": {}\n", json_optional_string(case.canary.as_deref())));
        out.push_str("  }");
    }
    out.push_str("\n]\n");
    out
}

pub fn report_to_json(report: &Report) -> String {
    let mut out = String::from("{\n");
    out.push_str(&format!("  \"schema_version\": {},\n", report.schema_version));
    out.push_str(&format!("  \"generated_at\": {},\n", json_string(&report.generated_at)));
    out.push_str("  \"target\": {\n");
    out.push_str(&format!("    \"provider\": {},\n", json_string(&report.target.provider)));
    out.push_str(&format!("    \"model\": {},\n", json_optional_string(report.target.model.as_deref())));
    out.push_str(&format!("    \"base_url\": {}\n", json_optional_string(report.target.base_url.as_deref())));
    out.push_str("  },\n");
    out.push_str("  \"summary\": {");
    for (index, (verdict, count)) in report.summary.iter().enumerate() {
        if index == 0 {
            out.push('\n');
        } else {
            out.push_str(",\n");
        }
        out.push_str(&format!("    \"{}\": {}", verdict.as_str(), count));
    }
    if report.summary.iter().next().is_some() {
        out.push_str("\n  ");
    }
    out.push_str("},\n");
    out.push_str("  \"results\": [\n");
    for (index, result) in report.results.iter().enumerate() {
        if index > 0 {
            out.push_str(",\n");
        }
        push_finding_json(&mut out, result);
    }
    out.push_str("\n  ]\n}\n");
    out
}

fn push_finding_json(out: &mut String, result: &Finding) {
    out.push_str("    {\n");
    out.push_str(&format!("      \"id\": {},\n", json_string(&result.case_id)));
    out.push_str(&format!("      \"category\": {},\n", json_string(&result.category)));
    out.push_str(&format!("      \"verdict\": {},\n", json_string(result.verdict.as_str())));
    out.push_str(&format!("      \"prompt\": {},\n", json_string(&result.prompt)));
    out.push_str(&format!("      \"response\": {},\n", json_string(&result.response)));
    out.push_str(&format!("      \"status\": {},\n", result.status.map_or_else(|| "null".into(), |value| value.to_string())));
    out.push_str(&format!("      \"latency_ms\": {}\n", result.latency_ms.map_or_else(|| "null".into(), |value| value.to_string())));
    out.push_str("    }");
}

pub fn build_report(target: Target, results: Vec<Finding>) -> Report {
    let summary = Summary::from_findings(&results);
    Report {
        schema_version: 1,
        generated_at: generated_at_utcish(),
        target,
        summary,
        results,
    }
}

fn generated_at_utcish() -> String {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(duration) => {
            let seconds = duration.as_secs() as i64;
            let days = seconds.div_euclid(86_400);
            let seconds_of_day = seconds.rem_euclid(86_400);
            let (year, month, day) = civil_from_days(days);
            let hour = seconds_of_day / 3_600;
            let minute = (seconds_of_day % 3_600) / 60;
            let second = seconds_of_day % 60;
            format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
        }
        Err(_) => "1970-01-01T00:00:00Z".into(),
    }
}

fn civil_from_days(days_since_unix_epoch: i64) -> (i64, i64, i64) {
    // Howard Hinnant's civil-from-days algorithm, adapted for proleptic UTC dates.
    let z = days_since_unix_epoch + 719_468;
    let era = (if z >= 0 { z } else { z - 146_096 }).div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096).div_euclid(365);
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2).div_euclid(153);
    let day = doy - (153 * mp + 2).div_euclid(5) + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = year + if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

fn json_optional_string(value: Option<&str>) -> String {
    value.map(json_string).unwrap_or_else(|| "null".into())
}

fn json_string(value: &str) -> String {
    let mut out = String::from("\"");
    for ch in value.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[derive(Debug, Clone, PartialEq)]
enum JsonValue {
    Object(BTreeMap<String, JsonValue>),
    Array(Vec<JsonValue>),
    String(String),
    Null,
    Bool,
    Number,
}

struct Parser<'a> {
    chars: Vec<char>,
    index: usize,
    source: &'a str,
}

impl<'a> Parser<'a> {
    fn new(source: &'a str) -> Self {
        Self { chars: source.chars().collect(), index: 0, source }
    }

    fn parse(mut self) -> Result<JsonValue, String> {
        let value = self.parse_value()?;
        self.skip_ws();
        if self.index != self.chars.len() {
            return Err(format!("unexpected trailing data at byte {}", self.byte_pos()));
        }
        Ok(value)
    }

    fn parse_value(&mut self) -> Result<JsonValue, String> {
        self.skip_ws();
        match self.peek() {
            Some('{') => self.parse_object(),
            Some('[') => self.parse_array(),
            Some('"') => self.parse_string().map(JsonValue::String),
            Some('n') => self.expect_literal("null", JsonValue::Null),
            Some('t') => self.expect_literal("true", JsonValue::Bool),
            Some('f') => self.expect_literal("false", JsonValue::Bool),
            Some('-') | Some('0'..='9') => self.parse_number(),
            Some(ch) => Err(format!("unexpected character '{ch}' at byte {}", self.byte_pos())),
            None => Err("unexpected end of JSON".into()),
        }
    }

    fn parse_object(&mut self) -> Result<JsonValue, String> {
        self.consume('{')?;
        let mut object = BTreeMap::new();
        loop {
            self.skip_ws();
            if self.try_consume('}') {
                break;
            }
            let key = self.parse_string()?;
            self.skip_ws();
            self.consume(':')?;
            let value = self.parse_value()?;
            object.insert(key, value);
            self.skip_ws();
            if self.try_consume('}') {
                break;
            }
            self.consume(',')?;
        }
        Ok(JsonValue::Object(object))
    }

    fn parse_array(&mut self) -> Result<JsonValue, String> {
        self.consume('[')?;
        let mut items = Vec::new();
        loop {
            self.skip_ws();
            if self.try_consume(']') {
                break;
            }
            items.push(self.parse_value()?);
            self.skip_ws();
            if self.try_consume(']') {
                break;
            }
            self.consume(',')?;
        }
        Ok(JsonValue::Array(items))
    }

    fn parse_string(&mut self) -> Result<String, String> {
        self.consume('"')?;
        let mut out = String::new();
        while let Some(ch) = self.next() {
            match ch {
                '"' => return Ok(out),
                '\\' => {
                    let escaped = self.next().ok_or_else(|| "unterminated escape sequence".to_string())?;
                    match escaped {
                        '"' => out.push('"'),
                        '\\' => out.push('\\'),
                        '/' => out.push('/'),
                        'b' => out.push('\u{0008}'),
                        'f' => out.push('\u{000c}'),
                        'n' => out.push('\n'),
                        'r' => out.push('\r'),
                        't' => out.push('\t'),
                        'u' => out.push(self.parse_unicode_escape()?),
                        other => return Err(format!("unsupported escape '\\{other}' at byte {}", self.byte_pos())),
                    }
                }
                other => out.push(other),
            }
        }
        Err("unterminated string".into())
    }

    fn parse_unicode_escape(&mut self) -> Result<char, String> {
        let mut value = 0u32;
        for _ in 0..4 {
            let ch = self.next().ok_or_else(|| "unterminated unicode escape".to_string())?;
            value = value * 16 + ch.to_digit(16).ok_or_else(|| format!("invalid unicode escape at byte {}", self.byte_pos()))?;
        }
        char::from_u32(value).ok_or_else(|| "invalid unicode scalar".into())
    }

    fn parse_number(&mut self) -> Result<JsonValue, String> {
        if self.peek() == Some('-') {
            self.index += 1;
        }
        self.consume_digits();
        if self.peek() == Some('.') {
            self.index += 1;
            self.consume_digits();
        }
        if matches!(self.peek(), Some('e') | Some('E')) {
            self.index += 1;
            if matches!(self.peek(), Some('+') | Some('-')) {
                self.index += 1;
            }
            self.consume_digits();
        }
        Ok(JsonValue::Number)
    }

    fn consume_digits(&mut self) {
        while matches!(self.peek(), Some('0'..='9')) {
            self.index += 1;
        }
    }

    fn expect_literal(&mut self, literal: &str, value: JsonValue) -> Result<JsonValue, String> {
        for expected in literal.chars() {
            self.consume(expected)?;
        }
        Ok(value)
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(' ' | '\n' | '\r' | '\t')) {
            self.index += 1;
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.index).copied()
    }

    fn next(&mut self) -> Option<char> {
        let value = self.peek()?;
        self.index += 1;
        Some(value)
    }

    fn consume(&mut self, expected: char) -> Result<(), String> {
        match self.next() {
            Some(actual) if actual == expected => Ok(()),
            Some(actual) => Err(format!("expected '{expected}' but found '{actual}' at byte {}", self.byte_pos())),
            None => Err(format!("expected '{expected}' but reached end of JSON")),
        }
    }

    fn try_consume(&mut self, expected: char) -> bool {
        if self.peek() == Some(expected) {
            self.index += 1;
            true
        } else {
            false
        }
    }

    fn byte_pos(&self) -> usize {
        self.chars.iter().take(self.index).map(|ch| ch.len_utf8()).sum::<usize>().min(self.source.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_array_or_wrapped_cases() {
        let cases = load_cases_from_str(r#"{"cases":[{"id":"one","prompt":"hi","canary":null}]}"#).unwrap();
        assert_eq!(cases[0].id, "one");
        assert_eq!(cases[0].category, "unknown");
    }

    #[test]
    fn writes_report_json() {
        let case = Case::new("one", "control_benign", "reply PONG", Some("PONG".into()));
        let finding = Finding::from_case(&case, Verdict::ControlOk, "PONG");
        let report = build_report(Target { provider: "dry-run".into(), model: None, base_url: None }, vec![finding]);
        let json = report_to_json(&report);
        assert!(json.contains("\"CONTROL_OK\": 1"));
        assert!(json.contains("\"schema_version\": 1"));
    }
}
