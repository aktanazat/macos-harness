import Foundation

/// The browser the browser harness drives, whose windows on the agents' display are the
/// harness's to place: Safari, or in Ladybird mode the Ladybird copies its installer makes
/// (`Ladybird Harness`, and the test copy `Ladybird Harness Test`). The harness's own sweeps
/// send its windows home and move windows holding only his tabs to his screen, so the companion
/// leaves them alone. Until 11-27 only Safari was listed, and the companion moved every Ladybird
/// agent window to his screen about a second after the harness put it on the Agents display.
/// The browser the harness does not drive is his alone: in Ladybird mode five of his Safari
/// windows sat on the Agents display, which no harness sweep looks at.
public enum HarnessBrowsers {
  /// Where `browser mode` keeps the harness's browser.
  public static let choice = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".local/share/browser-harness/browser.json")

  /// "ladybird" when the harness drives Ladybird, else "safari", its default.
  public static func mode(at url: URL = choice) -> String {
    guard let data = try? Data(contentsOf: url),
          let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          object["mode"] as? String == "ladybird" else { return "safari" }
    return "ladybird"
  }

  public static func places(bundleID: String?, mode: String = mode()) -> Bool {
    guard let bundleID else { return false }
    return mode == "ladybird" ? bundleID.hasPrefix("org.ladybird.Ladybird.Harness") : bundleID == "com.apple.Safari"
  }
}
