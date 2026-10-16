import XCTest

@testable import macos_harness_agent

// `PressCoordinator` never activates or raises an app itself; it searches with a limit of two
// (or `labelSearchLimit` with text), fails closed on anything but exactly one AXPress-capable
// match (a whole-label match may settle a substring tie, and when strict, only a search that
// saw every candidate counts), and samples frontmost state immediately before and after
// delegating to the injected `performPress` closure — reporting focus.changed only when a
// target that was not already frontmost becomes frontmost.

final class PressCoordinatorTests: XCTestCase {

  private let target: pid_t = 500
  private let bystander: pid_t = 600

  private func descriptor(handle: Int = 1, actions: [String] = ["AXPress"]) -> ElementDescriptor {
    ElementDescriptor(handle: handle, actions: actions)
  }

  private func found(
    _ matches: [ElementDescriptor], complete: Bool = true, visited: Int? = nil
  ) -> AXExecutor.QueryResult {
    AXExecutor.QueryResult(matches: matches, complete: complete, visited: visited ?? matches.count)
  }

  private final class RecordingPerformer {
    private(set) var invocations: [ElementDescriptor] = []
    var shouldSucceed = true
    func perform(_ descriptor: ElementDescriptor) throws {
      invocations.append(descriptor)
      if !shouldSucceed {
        throw NSError(domain: "PressCoordinatorTests", code: 1)
      }
    }
  }

  private final class ScriptedFrontmost {
    private var values: [pid_t?]
    init(_ values: [pid_t?]) { self.values = values }
    func next() -> pid_t? {
      guard !values.isEmpty else { return nil }
      return values.removeFirst()
    }
  }

  private func assertAgentErrorCode(
    _ expression: @autoclosure () throws -> ElementDescriptor,
    equals expectedCode: String,
    file: StaticString = #filePath,
    line: UInt = #line
  ) {
    XCTAssertThrowsError(try expression(), file: file, line: line) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)", file: file, line: line)
      }
      XCTAssertEqual(agentError.code, expectedCode, file: file, line: line)
    }
  }

  func testZeroMatchesFailsClosedWithoutPerforming() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps),
      equals: "element.unknown")
    XCTAssertTrue(performer.invocations.isEmpty)
  }

  func testZeroMatchesReportsHowFarTheSearchGot() {
    // A caller deciding whether to retry with a bigger max_nodes needs to know whether the
    // empty result came from a search that saw everything or one that was cut short.
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([], complete: false, visited: 500) },
      frontmostPID: { self.bystander },
      performPress: { _ in }
    )
    XCTAssertThrowsError(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps)
    ) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)")
      }
      XCTAssertEqual(agentError.code, "element.unknown")
      XCTAssertEqual(agentError.details?["complete"], .bool(false))
      XCTAssertEqual(agentError.details?["visited"], .number(500))
    }
  }

  func testStrictSingleMatchFromIncompleteSearchFailsClosedWithoutPerforming() {
    // An exact selector promises uniqueness. One match from a search that stopped at its node
    // budget proves nothing about a second match past the cut, so the press must not happen.
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()], complete: false, visited: 500) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(
      try PressCoordinator.run(targetPID: target, text: nil, strict: true, deadline: nil, deps)
    ) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)")
      }
      XCTAssertEqual(agentError.code, "element.unknown")
      XCTAssertEqual(agentError.details?["complete"], .bool(false))
      XCTAssertEqual(agentError.details?["visited"], .number(500))
    }
    XCTAssertTrue(performer.invocations.isEmpty, "an unproven match must not be pressed")
  }

  func testStrictSingleMatchFromCompleteSearchPresses() throws {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor(handle: 9)], complete: true, visited: 120) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(
      targetPID: target, text: nil, strict: true, deadline: nil, deps)
    XCTAssertEqual(match.handle, 9)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testNonStrictSingleMatchFromIncompleteSearchStillPresses() throws {
    // Substring presses keep their pre-existing contract: the first unique hit within the
    // press search wins even when the walk was cut short.
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor(handle: 3)], complete: false, visited: 500) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(
      targetPID: target, text: "Not Now", strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 3)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testMultipleMatchesFailsClosedWithBadRequestAndCount() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor(handle: 1), self.descriptor(handle: 2)]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps)
    ) { error in
      guard let agentError = error as? AgentError else {
        return XCTFail("expected an AgentError, got \(error)")
      }
      // Ambiguous (>1 match) is the caller's search criteria being too loose, not an
      // unknown element and not worth retrying -- it is always `bad_request`, and the
      // match count stays visible in the message since there is no generic wire "details"
      // field to carry it separately.
      XCTAssertEqual(agentError.code, "bad_request")
      XCTAssertTrue(
        agentError.message.contains("2"),
        "expected the match count in the message, got \(agentError.message)")
    }
    XCTAssertTrue(performer.invocations.isEmpty, "an ambiguous match set must never be pressed")
  }

  func testMissingAXPressActionFailsClosedWithUnsupportedOp() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor(actions: ["AXShowMenu"])]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    // A single, unambiguous match that simply cannot be pressed is `unsupported_op`, not
    // `element.unknown`: the element was found just fine, it just has no AXPress action.
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps),
      equals: "unsupported_op")
    XCTAssertTrue(performer.invocations.isEmpty, "an element without AXPress must not be pressed")
  }

  func testSingleMatchNotFrontmostAndStaysNotFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor(handle: 7)]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(
      targetPID: target, text: nil, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 7)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testTargetBecomingFrontmostAfterPressReportsFocusChanged() {
    let performer = RecordingPerformer()
    // Was not frontmost before the press, becomes frontmost immediately after it.
    let frontmost = ScriptedFrontmost([bystander, target])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps),
      equals: "focus.changed")
    XCTAssertEqual(
      performer.invocations.count, 1, "the press must still be attempted before the focus check")
  }

  func testTargetAlreadyFrontmostStayingFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    // Already frontmost before the press, and remains frontmost after it.
    let frontmost = ScriptedFrontmost([target, target])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(
      targetPID: target, text: nil, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testUnrelatedFrontmostChurnDoesNotReportFocusChanged() throws {
    // Frontmost moves between two other apps around the press; the target itself never
    // becomes frontmost, so this must not be reported as focus.changed.
    let performer = RecordingPerformer()
    let otherApp: pid_t = 700
    let frontmost = ScriptedFrontmost([bystander, otherApp])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(
      targetPID: target, text: nil, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testFailedPerformIsNeverReportedAsSuccess() {
    let performer = RecordingPerformer()
    performer.shouldSucceed = false
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(
      try PressCoordinator.run(targetPID: target, text: nil, strict: false, deadline: nil, deps)
    ) { error in
      XCTAssertEqual((error as NSError).domain, "PressCoordinatorTests")
      XCTAssertEqual((error as NSError).code, 1)
    }
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testSearchCannotAuthorizeAPressAfterSpendingTheDeadline() {
    var now = 10.0
    let performer = RecordingPerformer()
    let deps = PressCoordinator.Dependencies(
      search: { _ in
        now += 0.06
        return self.found([self.descriptor()])
      },
      frontmostPID: { self.bystander },
      performPress: performer.perform)

    XCTAssertThrowsError(try PressCoordinator.run(
      targetPID: target, text: nil, strict: true, deadline: 10.05, deps, monotonic: { now }
    )) { error in
      XCTAssertEqual((error as? AgentError)?.code, "timeout")
      XCTAssertEqual((error as? AgentError)?.details?["reason"],
                     .string("deadline_exhausted_before_dispatch"))
    }
    XCTAssertTrue(performer.invocations.isEmpty)
  }

  func testFrontmostReadCannotAuthorizeAPressAfterSpendingTheDeadline() {
    var now = 10.0
    let performer = RecordingPerformer()
    let deps = PressCoordinator.Dependencies(
      search: { _ in self.found([self.descriptor()]) },
      frontmostPID: {
        now += 0.06
        return self.bystander
      },
      performPress: performer.perform)

    XCTAssertThrowsError(try PressCoordinator.run(
      targetPID: target, text: nil, strict: true, deadline: 10.05, deps, monotonic: { now }
    )) { error in
      XCTAssertEqual((error as? AgentError)?.code, "timeout")
      XCTAssertEqual((error as? AgentError)?.details?["reason"],
                     .string("deadline_exhausted_before_dispatch"))
    }
    XCTAssertTrue(performer.invocations.isEmpty)
  }

  // MARK: - Whole-label tiebreak

  /// A pressable match whose `field` ("title" or "description") reads `label`.
  private func labelled(_ handle: Int, _ field: String, _ label: String) -> ElementDescriptor {
    ElementDescriptor(handle: handle, actions: ["AXPress"], fields: [field: .string(label)])
  }

  private func assertAmbiguousWithoutPressing(
    text: String, strict: Bool, _ search: @escaping (Int) -> AXExecutor.QueryResult,
    file: StaticString = #filePath, line: UInt = #line
  ) {
    let performer = RecordingPerformer()
    let deps = PressCoordinator.Dependencies(
      search: search, frontmostPID: { self.bystander }, performPress: performer.perform)
    assertAgentErrorCode(
      try PressCoordinator.run(
        targetPID: target, text: text, strict: strict, deadline: nil, deps),
      equals: "bad_request", file: file, line: line)
    XCTAssertTrue(performer.invocations.isEmpty, file: file, line: line)
  }

  func testWholeLabelMatchSettlesASubstringTie() throws {
    // Text is a substring search, so "Allow" also finds "Don't Allow". The one match labelled
    // exactly "Allow" -- by title or by description -- is the target, even from a search that
    // did not see every node, just as a substring press accepts one match from such a search.
    for field in ["title", "description"] {
      let performer = RecordingPerformer()
      let deps = PressCoordinator.Dependencies(
        search: { _ in
          self.found(
            [self.labelled(1, field, "Don't Allow"), self.labelled(2, field, "Allow")],
            complete: false, visited: 500)
        },
        frontmostPID: { self.bystander },
        performPress: performer.perform
      )
      let match = try PressCoordinator.run(
        targetPID: target, text: "Allow", strict: false, deadline: nil, deps)
      XCTAssertEqual(match.handle, 2, field)
      XCTAssertEqual(performer.invocations.map(\.handle), [2], field)
    }
  }

  func testTwoWholeLabelMatchesStayAmbiguous() {
    assertAmbiguousWithoutPressing(text: "Allow", strict: false) { _ in
      self.found([
        self.labelled(1, "title", "Allow"), self.labelled(2, "title", "Don't Allow"),
        self.labelled(3, "description", "Allow"),
      ])
    }
  }

  func testNoWholeLabelMatchStaysAmbiguous() {
    assertAmbiguousWithoutPressing(text: "Allow", strict: false) { _ in
      self.found([self.labelled(1, "title", "Allow All"), self.labelled(2, "title", "Don't Allow")])
    }
  }

  func testSearchThatFilledItsLimitCannotSettleATie() {
    // Every slot the limit allowed came back, so an unread match could be a second "Allow".
    assertAmbiguousWithoutPressing(text: "Allow", strict: false) { limit in
      self.found(
        [self.labelled(0, "title", "Allow")]
          + (1..<limit).map { self.labelled($0, "title", "Don't Allow \($0)") })
    }
  }

  func testStrictSearchMustBeCompleteToSettleATie() {
    // An exact selector promises uniqueness, which a search cut short cannot prove.
    assertAmbiguousWithoutPressing(text: "Allow", strict: true) { _ in
      self.found(
        [self.labelled(1, "title", "Allow"), self.labelled(2, "title", "Don't Allow")],
        complete: false, visited: 500)
    }
  }
}
