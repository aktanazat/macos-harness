import ApplicationServices
import Foundation
import XCTest

@testable import macos_harness_agent

/// `AXExecutor.requiredValue` is the strict single read behind the `ax_element_get_value`
/// wire op. These drive it with a scripted copy operation -- the `(status, value)` pair a real
/// `AXUIElementCopyAttributeValue` would hand back -- so no live target, trust grant, or
/// Accessibility prompt is involved, while the production status check and value conversion
/// run unchanged.
final class RequiredValueTests: XCTestCase {

  private let element = AXUIElementCreateSystemWide()

  private func read(_ status: AXError, _ value: AnyObject?) throws -> JSONValue {
    try AXExecutor.requiredValue(element, "AXValue", handle: 11) { _, _ in (status, value) }
  }

  func testRefusedReadFailsWithTheRawCodeInsteadOfReadingAsNull() {
    // `.noValue` and `.attributeUnsupported` are the two statuses the best-effort bulk reader
    // silently drops; here they are failures exactly like the local `MacOS.get` branch.
    let refusals: [(status: AXError, code: String)] = [
      (.cannotComplete, "timeout"),
      (.invalidUIElement, "ax.error"),
      (.noValue, "ax.error"),
      (.attributeUnsupported, "ax.error"),
    ]
    for (status, code) in refusals {
      XCTAssertThrowsError(try read(status, nil), "AXError \(status.rawValue) must not read") {
        error in
        guard let agentError = error as? AgentError else {
          return XCTFail("AXError \(status.rawValue): expected an AgentError, got \(error)")
        }
        XCTAssertEqual(agentError.code, code, "AXError \(status.rawValue)")
        XCTAssertEqual(agentError.axError, Int(status.rawValue), "raw code must travel with it")
      }
    }
  }

  func testSuccessfulFalseIsAValueNotARefusal() throws {
    XCTAssertEqual(try read(.success, false as AnyObject), .bool(false))
  }

  func testSuccessfulNumberIsAValueNotARefusal() throws {
    // Not 0 or 1: `jsonable` bridges those NSNumbers to `.bool`, a separate conversion
    // question this read boundary does not decide.
    XCTAssertEqual(try read(.success, NSNumber(value: 3)), .number(3))
  }

  func testSuccessfulNullStaysDistinguishableFromARefusedRead() throws {
    XCTAssertEqual(try read(.success, nil), .null)
  }
}
