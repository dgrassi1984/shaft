import Testing
@testable import SHAFTKit

@Test func pairingHTMLResponseDoesNotShowWebPage() {
    for body in ["<!doctype html><html lang=\"en\"><head></head></html>",
                 " \t\r\n<!doctype html><html lang=\"en\"><head></head></html>"] {
        let message = APIError.http(status: 403, body: body).message
        #expect(!message.contains("<"))
        #expect(!message.lowercased().contains("doctype"))
        #expect(message == "The server answered HTTP 403 with a web page, not the app API. It may not have /app/v1 yet: update its SHAFT web service.")
    }
    #expect(APIError.http(status: 403, body: "Access denied").message == "The server answered HTTP 403: Access denied")
    #expect(APIError.http(status: 403, body: "").message == "The server answered HTTP 403.")
}
