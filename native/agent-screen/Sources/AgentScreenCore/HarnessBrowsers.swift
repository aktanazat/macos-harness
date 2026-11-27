/// The browsers the browser harness drives, whose windows on the agents' display are the
/// harness's to place: Safari, and the Ladybird copies its installer makes (`Ladybird Harness`,
/// and the test copy `Ladybird Harness Test`). The harness's own sweeps send its windows home
/// and move windows holding only his tabs to his screen, so the companion leaves them alone.
/// Until 11-27 only Safari was listed, and the companion moved every Ladybird agent window to
/// his screen about a second after the harness put it on the Agents display.
public enum HarnessBrowsers {
  public static func places(bundleID: String?) -> Bool {
    guard let bundleID else { return false }
    return bundleID == "com.apple.Safari" || bundleID.hasPrefix("org.ladybird.Ladybird.Harness")
  }
}
