import XCTest

@testable import macos_harness_agent

// `PressCoordinator` never activates or raises an app itself; it searches with an effective
// limit of two, fails closed on anything but exactly one AXPress-capable match (and, when
// strict, only from a search that saw every candidate), and samples frontmost state
// immediately before and after delegating to the injected `performPress` closure — reporting
// focus.changed only when a target that was not already frontmost becomes frontmost.

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
      search: { self.found([]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps), equals: "element.unknown")
    XCTAssertTrue(performer.invocations.isEmpty)
  }

  func testZeroMatchesReportsHowFarTheSearchGot() {
    // A caller deciding whether to retry with a bigger max_nodes needs to know whether the
    // empty result came from a search that saw everything or one that was cut short.
    let deps = PressCoordinator.Dependencies(
      search: { self.found([], complete: false, visited: 500) },
      frontmostPID: { self.bystander },
      performPress: { _ in }
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)) { error in
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
      search: { self.found([self.descriptor()], complete: false, visited: 500) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, strict: true, deadline: nil, deps)) { error in
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
      search: { self.found([self.descriptor(handle: 9)], complete: true, visited: 120) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, strict: true, deadline: nil, deps)
    XCTAssertEqual(match.handle, 9)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testNonStrictSingleMatchFromIncompleteSearchStillPresses() throws {
    // Substring presses keep their pre-existing contract: the first unique hit within the
    // limit-two search wins even when the walk was cut short.
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor(handle: 3)], complete: false, visited: 500) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 3)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testMultipleMatchesFailsClosedWithBadRequestAndCount() {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor(handle: 1), self.descriptor(handle: 2)]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)) { error in
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
      search: { self.found([self.descriptor(actions: ["AXShowMenu"])]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    // A single, unambiguous match that simply cannot be pressed is `unsupported_op`, not
    // `element.unknown`: the element was found just fine, it just has no AXPress action.
    assertAgentErrorCode(
      try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps), equals: "unsupported_op")
    XCTAssertTrue(performer.invocations.isEmpty, "an element without AXPress must not be pressed")
  }

  func testSingleMatchNotFrontmostAndStaysNotFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor(handle: 7)]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 7)
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testTargetBecomingFrontmostAfterPressReportsFocusChanged() {
    let performer = RecordingPerformer()
    // Was not frontmost before the press, becomes frontmost immediately after it.
    let frontmost = ScriptedFrontmost([bystander, target])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    assertAgentErrorCode(try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps), equals: "focus.changed")
    XCTAssertEqual(
      performer.invocations.count, 1, "the press must still be attempted before the focus check")
  }

  func testTargetAlreadyFrontmostStayingFrontmostSucceeds() throws {
    let performer = RecordingPerformer()
    // Already frontmost before the press, and remains frontmost after it.
    let frontmost = ScriptedFrontmost([target, target])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testUnrelatedFrontmostChurnDoesNotReportFocusChanged() throws {
    // Frontmost moves between two other apps around the press; the target itself never
    // becomes frontmost, so this must not be reported as focus.changed.
    let performer = RecordingPerformer()
    let otherApp: pid_t = 700
    let frontmost = ScriptedFrontmost([bystander, otherApp])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    let match = try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)
    XCTAssertEqual(match.handle, 1)
  }

  func testFailedPerformIsNeverReportedAsSuccess() {
    let performer = RecordingPerformer()
    performer.shouldSucceed = false
    let frontmost = ScriptedFrontmost([bystander, bystander])
    let deps = PressCoordinator.Dependencies(
      search: { self.found([self.descriptor()]) },
      frontmostPID: { frontmost.next() },
      performPress: performer.perform
    )
    XCTAssertThrowsError(try PressCoordinator.run(targetPID: target, strict: false, deadline: nil, deps)) { error in
      XCTAssertEqual((error as NSError).domain, "PressCoordinatorTests")
      XCTAssertEqual((error as NSError).code, 1)
    }
    XCTAssertEqual(performer.invocations.count, 1)
  }

  func testSearchCannotAuthorizeAPressAfterSpendingTheDeadline() {
    var now = 10.0
    let performer = RecordingPerformer()
    let deps = PressCoordinator.Dependencies(
      search: {
        now += 0.06
        return self.found([self.descriptor()])
      },
      frontmostPID: { self.bystander },
      performPress: performer.perform)

    XCTAssertThrowsError(try PressCoordinator.run(
      targetPID: target, strict: true, deadline: 10.05, deps, monotonic: { now }
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
      search: { self.found([self.descriptor()]) },
      frontmostPID: {
        now += 0.06
        return self.bystander
      },
      performPress: performer.perform)

    XCTAssertThrowsError(try PressCoordinator.run(
      targetPID: target, strict: true, deadline: 10.05, deps, monotonic: { now }
    )) { error in
      XCTAssertEqual((error as? AgentError)?.code, "timeout")
      XCTAssertEqual((error as? AgentError)?.details?["reason"],
                     .string("deadline_exhausted_before_dispatch"))
    }
    XCTAssertTrue(performer.invocations.isEmpty)
  }
}
