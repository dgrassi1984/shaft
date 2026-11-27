import Foundation

/// What the recorders share about the files they leave behind (docs/meetings.md).
public enum MeetingFiles {
    /// A CAF recording cut short ends in an open `data` chunk (size -1) whose last packet may be half written; the
    /// server's decoder refuses that half packet, so it is cut off. Files that are not such a CAF are left alone.
    public static func trimPartialPacket(of file: URL) throws {
        let handle = try FileHandle(forUpdating: file)
        defer { try? handle.close() }
        let size = try handle.seekToEnd()
        func read(_ offset: UInt64, _ count: Int) throws -> Data? {
            try handle.seek(toOffset: offset)
            let data = try handle.read(upToCount: count) ?? Data()
            return data.count == count ? data : nil
        }
        func number(_ data: Data, _ range: Range<Int>) -> UInt64 {
            data[range].reduce(0) { $0 << 8 | UInt64($1) }
        }
        guard let head = try read(0, 8), head.prefix(4) == Data("caff".utf8) else { return }
        var offset: UInt64 = 8
        var bytesPerPacket: UInt64 = 0
        while offset + 12 <= size, let chunk = try read(offset, 12) {
            let type = String(decoding: chunk.prefix(4), as: UTF8.self)
            let length = number(chunk, 4..<12)
            if type == "desc", let desc = try read(offset + 12, 32) {
                bytesPerPacket = number(desc, 16..<20)
            }
            if type == "data" {
                let start = offset + 12 + 4  // after the edit count
                guard length == UInt64.max, bytesPerPacket > 0, size > start else { return }
                let whole = start + (size - start) / bytesPerPacket * bytesPerPacket
                if whole < size { try handle.truncate(atOffset: whole) }
                return
            }
            guard length != UInt64.max else { return }
            offset += 12 + length
        }
    }
}
