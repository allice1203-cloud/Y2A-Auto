import Foundation

var paths: [String] = []
while let line = readLine(strippingNewline: true) {
    let t = line.trimmingCharacters(in: .whitespaces)
    if !t.isEmpty { paths.append(t) }
}

let fm = FileManager.default
var requested = 0, notUbiquitous = 0, failed = 0, alreadyLocal = 0

for p in paths {
    let url = URL(fileURLWithPath: p)
    let vals = try? url.resourceValues(forKeys: [.isUbiquitousItemKey, .ubiquitousItemDownloadingStatusKey])
    guard let isUbiq = vals?.isUbiquitousItem, isUbiq else {
        notUbiquitous += 1
        continue
    }
    if vals?.ubiquitousItemDownloadingStatus == .current {
        alreadyLocal += 1
        continue
    }
    do {
        try fm.startDownloadingUbiquitousItem(at: url)
        requested += 1
    } catch {
        failed += 1
    }
}

let msg = "requested=\(requested) alreadyLocal=\(alreadyLocal) notUbiquitous=\(notUbiquitous) failed=\(failed)\n"
FileHandle.standardError.write(msg.data(using: .utf8)!)
