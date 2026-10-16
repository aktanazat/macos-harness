import Dispatch
import Foundation

/// Resolves and presses exactly one AX target on behalf of the `ax_press` wire operation.
///
/// `PressCoordinator` never activates or raises an application itself — the injected `search`
/// closure is expected to already be scoped to the target process, and is bounded by the limit
/// this coordinator asks for (`searchLimit(text:)`): two is just enough to prove ambiguity
/// without paying for an unbounded search, and a search with text reads `labelSearchLimit`
/// matches so `soleLabelMatch` can settle a substring tie, exactly like `MacOS.ax_wait` in
/// `macos.py`. This coordinator's own job is narrower — fail closed on anything but exactly
/// one AXPress-capable match, then sample frontmost state immediately before and immediately
/// after delegating to the injected `performPress` closure, reporting `focus.changed` only when
/// the target process was not already frontmost immediately before the press but became
/// frontmost immediately after it. That mirrors `MacOS._guard_focus` in `macos.py` one for one:
/// the press has already happened by the time focus is judged, so a background target that
/// steals focus is reported as an error even though its action already ran.
enum PressCoordinator {

  /// How many matches a press with search text reads: enough for `soleLabelMatch` to see every
  /// candidate a label search plausibly finds ("Allow" also finds "Don't Allow"). Mirrors
  /// `_AX_LABEL_SEARCH_LIMIT` in `macos.py`.
  static let labelSearchLimit = 20

  struct Dependencies {
    /// Finds at most `limit` candidate elements to press, with whether the search saw every
    /// candidate. Expected to already be scoped to the target process.
    let search: (_ limit: Int) throws -> AXExecutor.QueryResult
    /// Samples the current frontmost application's pid, or `nil` if none.
    let frontmostPID: () -> pid_t?
    /// Performs the actual `AXPress` action on the resolved element.
    let performPress: (ElementDescriptor) throws -> Void
  }

  /// The press search limit: `labelSearchLimit` with search text, otherwise two.
  static func searchLimit(text: String?) -> Int {
    guard let text, !text.isEmpty else { return 2 }
    return labelSearchLimit
  }

  /// The one match whose whole `title` or `description` is exactly `text`, when the search shows
  /// no other match can be: it stopped short of `limit` (an unread match could be a second exact
  /// label) and, when `strict`, was complete. Text is a substring search, so "Allow" also finds
  /// "Don't Allow"; two exact labels, or none, stay ambiguous. Mirrors
  /// `MacOS._sole_label_match` in `macos.py`.
  static func soleLabelMatch(
    text: String?, in result: AXExecutor.QueryResult, limit: Int, strict: Bool
  ) -> ElementDescriptor? {
    guard let text, !text.isEmpty, result.matches.count < limit, result.complete || !strict
    else { return nil }
    let label = JSONValue.string(text)
    let labelled = result.matches.filter {
      $0.fields["title"] == label || $0.fields["description"] == label
    }
    return labelled.count == 1 ? labelled[0] : nil
  }

  /// Throws `element.unknown` when the search finds no match at all -- or, when `strict`,
  /// finds one match from a search that stopped before it could rule out a second --
  /// `bad_request` when it finds more than one and `soleLabelMatch` cannot settle them
  /// (ambiguous — the caller's `search_key`/`text` must narrow further), or `unsupported_op`
  /// when the single match it does settle on does not expose `AXPress` -- in all three cases
  /// before `performPress` is ever invoked, so a caller retrying only `element.unknown` (see
  /// `MacOS._native_press` in `macos.py`) never wastes its deadline retrying an ambiguous or
  /// non-pressable target that another search will never resolve any differently. `strict` is
  /// how an exact selector presses: its uniqueness is only proven by a complete search, so
  /// `element.unknown` there carries `complete`/`visited` in `details` to say which bound
  /// stopped it. Once the press is attempted, any error `performPress` throws propagates
  /// unchanged: a failed press is never reported as a success. A successful press that made a
  /// background target frontmost throws a `focus.changed` `AgentError` despite the press itself
  /// already having happened.
  static func run(
    targetPID: pid_t, text: String?, strict: Bool, deadline: Double?, _ deps: Dependencies,
    monotonic: () -> Double = { Double(DispatchTime.now().uptimeNanoseconds) / 1_000_000_000 }
  ) throws -> ElementDescriptor {
    func checkDeadline() throws {
      if let deadline, monotonic() >= deadline {
        throw AgentError(
          code: "timeout", message: "AX press deadline exhausted before dispatch",
          details: ["reason": .string("deadline_exhausted_before_dispatch")])
      }
    }
    try checkDeadline()
    let limit = searchLimit(text: text)
    let result = try deps.search(limit)
    let matches = result.matches
    let details: [String: JSONValue] = [
      "complete": .bool(result.complete), "visited": .number(Double(result.visited)),
    ]
    guard !matches.isEmpty else {
      throw AgentError(
        code: "element.unknown",
        message:
          "AX press expected exactly one match for pid \(targetPID), found 0",
        details: details
      )
    }
    let match: ElementDescriptor
    if matches.count == 1 {
      if strict, !result.complete {
        throw AgentError(
          code: "element.unknown",
          message:
            "AX press found one match for pid \(targetPID) but the search stopped "
            + "before confirming it is unique; raise max_nodes or narrow the search",
          details: details
        )
      }
      match = matches[0]
    } else if let labelled = soleLabelMatch(
      text: text, in: result, limit: limit, strict: strict)
    {
      match = labelled
    } else {
      throw AgentError(
        code: "bad_request",
        message:
          "AX press expected exactly one match for pid \(targetPID), found "
          + "\(matches.count) (ambiguous); narrow search_key/text to a unique target"
      )
    }
    guard match.actions.contains("AXPress") else {
      throw AgentError(
        code: "unsupported_op",
        message:
          "AX press target does not expose AXPress; available actions: \(match.actions)"
      )
    }

    let wasFrontmost = deps.frontmostPID() == targetPID
    try checkDeadline()
    try deps.performPress(match)
    let isFrontmost = deps.frontmostPID() == targetPID

    if !wasFrontmost && isFrontmost {
      throw AgentError(
        code: "focus.changed",
        message: "Process \(targetPID) became frontmost during AX press; stopped"
      )
    }
    return match
  }
}
