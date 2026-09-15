import ApplicationServices
import XCTest

@testable import macos_harness_agent

// `AXExecutor.ExactSelector` decides the exact-equality half of a search: substring `text`
// narrows the candidates, this accepts or rejects each one. It must agree with `_ExactSelector`
// in `macos.py` on every input, because the Python client runs the same predicate over the
// bounded-walk fallback when the agent is unavailable.

final class ExactSelectorTests: XCTestCase {

  func testUnsetSelectorAcceptsAnyFields() {
    let selector = AXExecutor.ExactSelector.none
    XCTAssertTrue(selector.matches([:]))
    XCTAssertTrue(selector.matches(["title": .string("anything")]))
  }

  func testTitleIsCompared_WholeAndCaseSensitive() {
    // Substring search would accept "Save" for "Save As…"; exact must not, and it must not
    // fold case either -- that is what makes it a selector an agent can trust.
    let selector = AXExecutor.ExactSelector(title: "Save", identifier: nil, description: nil)
    XCTAssertTrue(selector.matches(["title": .string("Save")]))
    XCTAssertFalse(selector.matches(["title": .string("Save As…")]))
    XCTAssertFalse(selector.matches(["title": .string("save")]))
    XCTAssertFalse(selector.matches(["title": .string(" Save")]))
  }

  func testMissingOrNonStringAttributeNeverMatches() {
    let selector = AXExecutor.ExactSelector(title: nil, identifier: "_NS:9", description: nil)
    XCTAssertFalse(selector.matches([:]), "an element without the attribute is not a match")
    XCTAssertFalse(selector.matches(["identifier": .null]))
    XCTAssertFalse(selector.matches(["identifier": .number(9)]))
    XCTAssertTrue(selector.matches(["identifier": .string("_NS:9")]))
  }

  func testEverySetSelectorMustMatchTogether() {
    let selector = AXExecutor.ExactSelector(
      title: "Save", identifier: "_NS:9", description: "Save the document")
    XCTAssertTrue(
      selector.matches([
        "title": .string("Save"), "identifier": .string("_NS:9"),
        "description": .string("Save the document"),
      ]))
    XCTAssertFalse(
      selector.matches([
        "title": .string("Save"), "identifier": .string("_NS:9"),
        "description": .string("Save a copy"),
      ]), "one wrong field rejects the element even when the others agree")
  }

  func testUnsetFieldsDoNotConstrainAMatch() {
    let selector = AXExecutor.ExactSelector(title: nil, identifier: nil, description: "Close")
    XCTAssertTrue(
      selector.matches(["description": .string("Close"), "title": .string("unrelated")]),
      "an unset selector must not constrain the element")
  }

  func testRefusedReadKeepsKnownValuesButLeavesCompletenessUnproved() {
    var reading = AXExecutor.AttributeValues()
    reading.record("AXTitle", status: .success, value: "Save" as NSString)
    reading.record("AXChildren", status: .cannotComplete, value: nil)
    reading.record("AXRole", status: .success, value: "AXButton" as NSString)

    XCTAssertEqual(reading.values["AXTitle"] as? String, "Save")
    XCTAssertFalse(reading.complete)
  }

  func testMissingAndUnsupportedAttributesAreKnownAbsences() {
    var reading = AXExecutor.AttributeValues()
    reading.record("AXChildren", status: .noValue, value: nil)
    reading.record("AXWindows", status: .attributeUnsupported, value: nil)

    XCTAssertTrue(reading.complete)
    XCTAssertTrue(reading.values.isEmpty)
  }
}
