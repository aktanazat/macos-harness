import AgentScreenCore
import CoreGraphics
import Foundation
import XCTest

final class ClaimsFileTests: XCTestCase {
  /// Lines as agent-display writes them: sets in any order, `-` for none, `quitting` for an
  /// app the companion is quitting.
  func testParsesAgentDisplayLines() {
    let text = "812 7 4021,977 1 1 77,12\n913 8 - 0 0 -\n914 9 quitting 1 1 3\n-1 5 9 1 1 -\nnot a claim\n"
    XCTAssertEqual(ClaimsFile.parse(text, startOf: { _ in nil }), [
      Claim(pid: 812, start: 7, state: .active(owners: [977, 4021]), launched: true, wholeApp: true,
            windows: [12, 77]),
      Claim(pid: 913, start: 8, state: .active(owners: []), launched: false, wholeApp: false, windows: []),
      Claim(pid: 914, start: 9, state: .quitting, launched: true, wholeApp: true, windows: [3]),
    ])
  }

  func testWritesWhatItReads() {
    let claims = [
      Claim(pid: 5, start: 11, state: .active(owners: [9, 3]), launched: true, wholeApp: false, windows: [40, 2]),
      Claim(pid: 6, start: 12, state: .quitting, launched: true, wholeApp: true, windows: []),
    ]
    XCTAssertEqual(ClaimsFile.format(claims), "5 11 3,9 1 0 2,40\n6 12 quitting 1 1 -")
    XCTAssertEqual(ClaimsFile.parse(ClaimsFile.format(claims), startOf: { _ in nil }), claims)
  }

  /// A line agent-display wrote before start times belongs to the process holding its pid now;
  /// with no such process it is no claim.
  func testReadsLinesWithoutAStartTime() {
    let text = "812 4021 1 1 77\n913 - 0 0 -\n"
    XCTAssertEqual(ClaimsFile.parse(text, startOf: { $0 == 812 ? 44 : nil }), [
      Claim(pid: 812, start: 44, state: .active(owners: [4021]), launched: true, wholeApp: true, windows: [77]),
    ])
  }

  /// A window an agent app opens later joins its claim and keeps who owns it.
  func testAddingKeepsOwnersAndLaunch() {
    let prior = [Claim(pid: 5, start: 11, state: .active(owners: [9]), launched: true, wholeApp: true, windows: [1])]
    XCTAssertEqual(ClaimsFile.adding(prior, pid: 5, start: 11, windows: [2], wholeApp: true, launched: false),
                   [Claim(pid: 5, start: 11, state: .active(owners: [9]), launched: true, wholeApp: true,
                          windows: [1, 2])])
  }

  func testAnAppBeingQuitTakesNoMoreWindows() {
    let prior = [Claim(pid: 5, start: 11, state: .quitting, launched: true, wholeApp: true, windows: [1])]
    XCTAssertEqual(ClaimsFile.adding(prior, pid: 5, start: 11, windows: [2], wholeApp: true, launched: false), prior)
  }

  /// The sweep ends a claim only as it read it: an owner who joined since keeps the app.
  func testEndingRefusesAClaimChangedSinceItWasRead() {
    let read = Claim(pid: 5, start: 11, state: .active(owners: [9]), launched: true, wholeApp: true, windows: [1])
    let joined = Claim(pid: 5, start: 11, state: .active(owners: [9, 10]), launched: true, wholeApp: true,
                       windows: [1])
    XCTAssertNil(ClaimsFile.ending([joined], read, quitting: true))
    XCTAssertEqual(ClaimsFile.ending([read], read, quitting: true),
                   [Claim(pid: 5, start: 11, state: .quitting, launched: true, wholeApp: true, windows: [1])])
    XCTAssertEqual(ClaimsFile.ending([read], read, quitting: false), [])
  }

  /// An app that would not quit (a save sheet) is released at the next sweep and is his from then
  /// on; apps of ended agents are quit; apps with a live owner stay.
  func testSweepReleasesAnAppThatDidNotQuit() {
    let stuck = Claim(pid: 5, start: 11, state: .quitting, launched: true, wholeApp: true, windows: [1])
    let ended = Claim(pid: 6, start: 12, state: .active(owners: [90]), launched: true, wholeApp: true, windows: [2])
    let live = Claim(pid: 7, start: 13, state: .active(owners: [91]), launched: true, wholeApp: true, windows: [3])
    XCTAssertEqual(ClaimsFile.sweep([stuck, ended, live], ownerAlive: { $0 == 91 }),
                   [.release(stuck), .quit(ended)])
  }
}

final class ClaimsStoreTests: XCTestCase {
  private var directory: URL!

  override func setUpWithError() throws {
    directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
  }

  override func tearDownWithError() throws {
    try FileManager.default.removeItem(at: directory)
  }

  /// Two writers adding windows to two apps at once keep every window.
  func testConcurrentWritersLoseNothing() throws {
    let store = ClaimsStore(directory: directory)
    let apps = [getpid(), getppid()]
    let starts = try apps.map { try XCTUnwrap(ProcessStart.of($0)) }
    DispatchQueue.concurrentPerform(iterations: 2) { n in
      for i in 0..<200 {
        store.update { claims in
          claims = ClaimsFile.adding(claims, pid: apps[n], start: starts[n], windows: [UInt32(n * 1000 + i)],
                                     wholeApp: true, launched: true)
        }
      }
    }
    XCTAssertEqual(store.all().map(\.windows.count).sorted(), [200, 200])
  }

  /// A line for a process that ended, or for an earlier process given the same pid, is no claim
  /// and goes at the next write.
  func testOnlyTheClaimedProcessHoldsTheClaim() throws {
    let start = try XCTUnwrap(ProcessStart.of(getpid()))
    let lines = "\(getpid()) \(start) 9 1 1 1\n\(getppid()) 1 9 1 1 2\n"
    try lines.write(to: directory.appendingPathComponent("claims"), atomically: true, encoding: .utf8)
    let store = ClaimsStore(directory: directory)
    XCTAssertEqual(store.all().map(\.pid), [getpid()])
    store.update { _ in }
    let kept = try String(contentsOf: directory.appendingPathComponent("claims"), encoding: .utf8)
    XCTAssertEqual(kept, "\(getpid()) \(start) 9 1 1 1")
  }
}

final class GeometryTests: XCTestCase {
  func testAgentDisplaysSitPastTheBottomRightCorner() {
    let real = [CGRect(x: 0, y: 0, width: 2560, height: 1440), CGRect(x: -1920, y: 200, width: 1920, height: 1080)]
    XCTAssertEqual(Geometry.cornerOrigins(real: real, agentWidths: [2560, 1280]),
                   [CGPoint(x: 2560, y: 1440), CGPoint(x: 5120, y: 1440)])
  }

  func testNoRealScreenPlacesNothing() {
    XCTAssertNil(Geometry.cornerOrigins(real: [], agentWidths: [2560]))
  }

  // The work Mac on 2026-11-18: laptop below a wide monitor; macOS snapped the agent display
  // to the laptop's corner, and treating that as wrong re-placed it every few seconds.
  func testCornerOnlyContactIsNotABorder() {
    let laptop = CGRect(x: 0, y: 0, width: 1352, height: 878)
    let monitor = CGRect(x: -1055, y: -1440, width: 3440, height: 1440)
    let agents = CGRect(x: 1352, y: 878, width: 2560, height: 1440)
    XCTAssertFalse(Geometry.sharesEdge(agents, laptop))
    XCTAssertFalse(Geometry.sharesEdge(agents, monitor))
    XCTAssertTrue(Geometry.sharesEdge(CGRect(x: 1352, y: 0, width: 2560, height: 1440), laptop))
  }

  func testWindowKeepsItsPlaceOnTheUsersScreen() {
    let agent = CGRect(x: 2560, y: 1440, width: 2560, height: 1440)
    let area = CGRect(x: 0, y: 25, width: 1512, height: 920)
    XCTAssertEqual(Geometry.userFrame(window: CGRect(x: 2660, y: 1540, width: 800, height: 600), agentBounds: agent, area: area),
                   CGRect(x: 100, y: 125, width: 800, height: 600))
    XCTAssertEqual(Geometry.userFrame(window: CGRect(x: 4000, y: 1500, width: 2000, height: 1300), agentBounds: agent, area: area),
                   CGRect(x: 0, y: 25, width: 1512, height: 920))
  }

  func testPeekClicksMapToTheDisplayAndBack() {
    let display = CGRect(x: 2560, y: 1440, width: 2560, height: 1440)
    let shown = CGRect(x: 4, y: 4, width: 640, height: 360)
    let point = Geometry.displayPoint(viewPoint: CGPoint(x: 164, y: 274), shown: shown, display: display)
    XCTAssertEqual(point, CGPoint(x: 3200, y: 1800))
    XCTAssertEqual(Geometry.viewPoint(displayPoint: CGPoint(x: 3200, y: 1800), shown: shown, display: display), CGPoint(x: 164, y: 274))
    XCTAssertNil(Geometry.displayPoint(viewPoint: CGPoint(x: 700, y: 10), shown: shown, display: display))
  }
}

final class GrabBookTests: XCTestCase {
  private let t0 = Date(timeIntervalSince1970: 1_000_000)
  private let home = CGRect(x: 2600, y: 1500, width: 900, height: 700)

  func testUnusedWindowGoesBackAfterTwoMinutes() {
    var book = GrabBook()
    book.grab(windowID: 7, pid: 40, home: home, safari: false, at: t0)
    XCTAssertEqual(book.due(at: t0.addingTimeInterval(119)).map(\.windowID), [])
    XCTAssertEqual(book.due(at: t0.addingTimeInterval(120)).map(\.windowID), [7])
  }

  func testUseKeepsItOnHisScreen() {
    var book = GrabBook()
    book.grab(windowID: 7, pid: 40, home: home, safari: false, at: t0)
    book.used(7, at: t0.addingTimeInterval(100))
    XCTAssertEqual(book.due(at: t0.addingTimeInterval(200)).map(\.windowID), [])
    XCTAssertEqual(book.due(at: t0.addingTimeInterval(220)).map(\.windowID), [7])
  }

  /// The Safari helper takes an agent window it finds off the agents' screen at its 60 s check
  /// as moved there by him.
  func testSafariWindowGoesBackBeforeTheHelpersCheck() {
    var book = GrabBook()
    book.grab(windowID: 8, pid: 41, home: home, safari: true, at: t0)
    XCTAssertEqual(book.due(at: t0.addingTimeInterval(59)).map(\.windowID), [8])
  }

  func testSecondGrabKeepsTheFirstHome() {
    var book = GrabBook()
    book.grab(windowID: 7, pid: 40, home: home, safari: false, at: t0)
    book.grab(windowID: 7, pid: 40, home: CGRect(x: 10, y: 40, width: 900, height: 700), safari: false, at: t0.addingTimeInterval(5))
    XCTAssertEqual(book.release(7)?.home, home)
    XCTAssertNil(book.release(7))
  }

  func testClosedWindowsAreForgotten() {
    var book = GrabBook()
    book.grab(windowID: 7, pid: 40, home: home, safari: false, at: t0)
    book.grab(windowID: 9, pid: 40, home: home, safari: false, at: t0)
    book.keep(alive: [9])
    XCTAssertEqual(book.grabs.map(\.windowID), [9])
  }
}

final class TrailTests: XCTestCase {
  private let t0 = Date(timeIntervalSince1970: 1_000_000)

  private func entry(_ text: String, sender: pid_t, at seconds: TimeInterval, receipt: Bool = false, agent: String = "a") -> TrailEntry {
    TrailEntry(time: t0.addingTimeInterval(seconds), agent: agent, senderPid: sender, appPid: 99, fromReceipt: receipt,
               text: text, count: 1)
  }

  /// A `mac.do.type` sends its own keys through `mac.type`; the trail shows the one line
  /// that names the field, not both.
  func testReceiptReplacesTheRawInputItSent() {
    var trail = Trail()
    trail.add(entry("clicked in Notes", sender: 1, at: 0))
    trail.add(entry("pressed cmd+a in Notes", sender: 1, at: 8.8))
    trail.add(entry("typed in Notes", sender: 2, at: 9.5, agent: "b"))
    trail.add(entry("typed in Notes", sender: 1, at: 9.5))
    trail.add(entry("typed into “Body” in Notes", sender: 1, at: 10, receipt: true), receiptDuration: 1)
    XCTAssertEqual(trail.entries.map(\.text),
                   ["typed into “Body” in Notes", "typed in Notes", "pressed cmd+a in Notes", "clicked in Notes"])
    XCTAssertEqual(trail.entries.map(\.senderPid), [1, 2, 1, 1])
  }

  func testRepeatsCountUpOneLine() {
    var trail = Trail()
    trail.add(entry("pressed a key in Notes", sender: 1, at: 0))
    trail.add(entry("pressed a key in Notes", sender: 1, at: 1))
    trail.add(entry("pressed a key in Notes", sender: 1, at: 20))
    XCTAssertEqual(trail.entries.map(\.count), [1, 2])
  }

  func testWordingNamesTheControlNotTheText() {
    let field = ActivityEvent.Target(role: "AXSecureTextField", title: nil, description: nil)
    XCTAssertEqual(TrailWording.phrase(op: "fill", target: field, app: "Safari", window: nil, key: nil, failed: false),
                   "typed into a password field in Safari")
    let button = ActivityEvent.Target(role: "AXButton", title: "Save", description: nil)
    XCTAssertEqual(TrailWording.phrase(op: "click", target: button, app: "TextEdit", window: "Untitled", key: nil, failed: true),
                   "tried: clicked “Save” in TextEdit")
    XCTAssertEqual(TrailWording.phrase(op: "click", target: nil, app: "TextEdit", window: "Untitled", key: nil, failed: false),
                   "clicked in TextEdit — Untitled")
  }

  func testDecodesWhatTheHarnessSends() {
    let data = Data(#"{"v":1,"kind":"action","pid":12,"agent":"codex","t":5.5,"op":"type","source":"raw","app_pid":40,"window_id":7,"target":null,"outcome":"succeeded","duration_s":null,"key":null}"#.utf8)
    let e = ActivityEvent.decode(data)
    XCTAssertEqual(e?.appPid, 40)
    XCTAssertEqual(e?.windowID, 7)
    XCTAssertEqual(e?.agent, "codex")
    XCTAssertNil(ActivityEvent.decode(Data(#"{"v":2,"kind":"action","pid":1,"t":0}"#.utf8)))
  }
}

final class LabelsTests: XCTestCase {
  func testSafariTitlesNameTheAgentOrGroup() {
    XCTAssertEqual(Labels.safariOwner(title: "agent 99234, window 2 — Opinion | The Times").agentPid, 99234)
    XCTAssertEqual(Labels.safariOwner(title: "web search (agent) — web search (agent)").group, "web search (agent)")
    XCTAssertEqual(Labels.safariOwner(title: "omp · authentication").group, "omp · authentication")
  }

  func testWaitingOnHimMeansSignInOrRobotCheck() {
    XCTAssertTrue(Labels.needsUser(title: "omp · authentication"))
    XCTAssertTrue(Labels.needsUser(title: "agent 4, window 1 — Just a moment..."))
    XCTAssertFalse(Labels.needsUser(title: "agent 4, window 1 — The New York Times"))
  }

  /// Values from macos_harness.pointer.PointerIdentity(name).color, so an arrow in the peek has
  /// the colour of the live one.
  func testColourMatchesTheHarnessArrow() {
    let cases: [(String, (Double, Double, Double))] = [
      ("codex", (0.1624, 0.58, 0.18721144790649413)),
      ("AgentScreen", (0.1624, 0.2763015035629272, 0.58)),
      ("omp · macos-harness", (0.3888206661224365, 0.1624, 0.58)),
    ]
    for (name, want) in cases {
      let got = Labels.color(of: name)
      XCTAssertEqual(got.red, want.0, accuracy: 1e-9, name)
      XCTAssertEqual(got.green, want.1, accuracy: 1e-9, name)
      XCTAssertEqual(got.blue, want.2, accuracy: 1e-9, name)
    }
  }
}

final class PowerTests: XCTestCase {
  /// Pauses only an idle agent app in the background with no window of his open in it.
  func testOnlyIdleBackgroundAgentAppsPause() {
    let now = Date(timeIntervalSince1970: 1_000_000)
    let idle = now.addingTimeInterval(-601)
    func app(_ pid: pid_t, launched: Bool = true, frontmost: Bool = false, grabbed: Bool = false,
             hasUserWindow: Bool = false, lastActivity: Date) -> Power.App {
      Power.App(pid: pid, launched: launched, frontmost: frontmost, grabbed: grabbed, hasUserWindow: hasUserWindow,
                lastActivity: lastActivity)
    }
    let apps = [
      app(1, lastActivity: idle),
      app(2, launched: false, lastActivity: idle),
      app(3, frontmost: true, lastActivity: idle),
      app(4, grabbed: true, lastActivity: idle),
      app(5, lastActivity: now.addingTimeInterval(-300)),
      app(6, hasUserWindow: true, lastActivity: idle),
    ]
    XCTAssertEqual(Power.toPause(apps, now: now), [1])
  }
}

final class FrontmostTests: XCTestCase {
  /// The harness brings a claimed app forward in a pause of his typing: his last key is under
  /// 2 s old, but the agent acted on the app just now, so its windows stay with the agents.
  func testAnAgentsBringForwardWhileHeTypesIsNotHis() {
    XCTAssertFalse(Frontmost.isHis(appClaimed: true, sincePerson: 0.5, sinceAgentActed: 0.1))
  }

  func testAClaimedAppThatCameForwardByItselfIsNotHis() {
    XCTAssertFalse(Frontmost.isHis(appClaimed: true, sincePerson: 60, sinceAgentActed: nil))
  }

  func testHeBringsAClaimedAppForward() {
    XCTAssertTrue(Frontmost.isHis(appClaimed: true, sincePerson: 0.5, sinceAgentActed: 30))
  }

  func testAnAppNoAgentClaimedIsHis() {
    XCTAssertTrue(Frontmost.isHis(appClaimed: false, sincePerson: 60, sinceAgentActed: 0.1))
  }
}

/// The browser harness drives Safari or Ladybird Harness; on 11-27 the companion moved every
/// Ladybird agent window to his screen a second after the harness put it on the Agents display,
/// and in Ladybird mode left five of his Safari windows on the Agents display.
final class HarnessBrowsersTests: XCTestCase {
  func testInLadybirdModeTheHarnessPlacesItsLadybirdCopiesAndNotSafari() {
    XCTAssertTrue(HarnessBrowsers.places(bundleID: "org.ladybird.Ladybird.Harness", mode: "ladybird"))
    XCTAssertTrue(HarnessBrowsers.places(bundleID: "org.ladybird.Ladybird.HarnessTest", mode: "ladybird"))
    XCTAssertFalse(HarnessBrowsers.places(bundleID: "com.apple.Safari", mode: "ladybird"))
    XCTAssertFalse(HarnessBrowsers.places(bundleID: "org.ladybird.Ladybird", mode: "ladybird"))
    XCTAssertFalse(HarnessBrowsers.places(bundleID: nil, mode: "ladybird"))
  }

  func testInSafariModeTheHarnessPlacesSafariAlone() {
    XCTAssertTrue(HarnessBrowsers.places(bundleID: "com.apple.Safari", mode: "safari"))
    XCTAssertFalse(HarnessBrowsers.places(bundleID: "org.ladybird.Ladybird.Harness", mode: "safari"))
    XCTAssertFalse(HarnessBrowsers.places(bundleID: "com.google.Chrome", mode: "safari"))
  }

  func testTheModeIsLadybirdOnlyWhenTheChoiceSaysSo() throws {
    let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: dir) }
    let file = dir.appendingPathComponent("browser.json")
    XCTAssertEqual(HarnessBrowsers.mode(at: file), "safari")
    try Data(#"{"bundle":"org.ladybird.Ladybird.Harness","mode":"ladybird"}"#.utf8).write(to: file)
    XCTAssertEqual(HarnessBrowsers.mode(at: file), "ladybird")
    try Data(#"{"mode":"safari"}"#.utf8).write(to: file)
    XCTAssertEqual(HarnessBrowsers.mode(at: file), "safari")
    try Data("not json".utf8).write(to: file)
    XCTAssertEqual(HarnessBrowsers.mode(at: file), "safari")
  }
}
