import Foundation
import Testing
@testable import SHAFTKit

struct VoiceModelsTests {
    @Test func streamEventsKeepEveryPayloadIncludingNullFailure() throws {
        let data = try DecodingTests.fixture("stream")
        let events = try SHAFTJSON.decoder().decode([ChatStreamEvent].self, from: data)
        #expect(events.map(\.type) == [.status, .draft, .stepEnd, .toolStart, .toolEnd,
                                     .draft, .stepEnd, .answer, .end])
        let original = try #require(JSONSerialization.jsonObject(with: data) as? NSArray)
        let encoded = try #require(JSONSerialization.jsonObject(
            with: SHAFTJSON.encoder().encode(events)) as? NSArray)
        #expect(encoded == original)
    }

    @Test func streamAcceptsNullPayloadsAndFailureNotes() throws {
        let json = #"""
        [{"seq":1,"type":"status","reply":"3f0c9e21a4b7d658","phase":null,"detail":null},
         {"seq":2,"type":"tool_end","reply":"3f0c9e21a4b7d658","name":null,"ok":null},
         {"seq":3,"type":"end","reply":"3f0c9e21a4b7d658","failure":"stopped by you"}]
        """#
        let events = try SHAFTJSON.decoder().decode([ChatStreamEvent].self, from: Data(json.utf8))
        #expect(events[0].phase == nil)
        #expect(events[0].detail == nil)
        #expect(events[1].name == nil)
        #expect(events[1].ok == nil)
        #expect(events[2].failure == "stopped by you")
        let original = try #require(JSONSerialization.jsonObject(with: Data(json.utf8)) as? NSArray)
        let encoded = try #require(JSONSerialization.jsonObject(
            with: SHAFTJSON.encoder().encode(events)) as? NSArray)
        #expect(encoded == original)
    }

    @Test func voicesDecodeTheSection17Shape() throws {
        // §17 gives a field table, not a complete JSON example or a server fixture.
        let json = #"""
        {"voices":{"en":{"identifier":"example.en","name":"Example","by":"operator",
          "ts":"2026-11-03T13:02:11Z"},"it":null},
         "proposals":[{"id":"proposal-1","language":"it","identifier":"example.it",
          "name":"Example Italian","why":"Clear speech","ts":"2026-11-03T13:02:11Z","state":"open"}],
         "available":{"ios":[{"identifier":"example.en","name":"Example","language":"en-US",
          "quality":"enhanced"}],"macos":[]}}
        """#
        let response = try SHAFTJSON.decoder().decode(VoicesResponse.self, from: Data(json.utf8))
        let voice = try #require(response.voices["en"] ?? nil)
        #expect(voice.identifier == "example.en")
        #expect(voice.ts == SHAFTJSON.parseStamp("2026-11-03T14:02:11+01:00"))
        #expect(response.voices.keys.contains("it"))
        #expect(response.voices["it"]! == nil)
        #expect(response.proposals.first?.language == "it")
        #expect(response.available["ios"]?.first?.quality == "enhanced")
        #expect(response.available["macos"] == [])
        let original = try #require(JSONSerialization.jsonObject(with: Data(json.utf8)) as? NSDictionary)
        let encoded = try #require(JSONSerialization.jsonObject(
            with: SHAFTJSON.encoder().encode(response)) as? NSDictionary)
        #expect(encoded == original)
    }
}
