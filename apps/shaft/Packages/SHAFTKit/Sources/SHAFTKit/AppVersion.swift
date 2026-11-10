/// The app's own version as Settings shows it: the git label the build stamped (Scripts/stamp-version.sh), the same
/// `git describe` the server reports, so the two read alike.
public enum AppVersion {
    /// `SHAFTVersion`, or "x.y.z (build)" in a bundle the stamp did not reach (a test host).
    public static func label(info: [String: Any]?) -> String? {
        if let version = info?["SHAFTVersion"] as? String { return version }
        guard let version = info?["CFBundleShortVersionString"] as? String,
              let build = info?["CFBundleVersion"] as? String else { return nil }
        return "\(version) (\(build))"
    }
}
