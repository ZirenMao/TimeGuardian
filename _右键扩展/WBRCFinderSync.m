// WBRCFinderSync.m —— WorkBuddy 右键扩展（真正的实现，不是探针）
// ============================================================================
// 定位：FinderSync 扩展**只做 UI + 丢任务文件**，重活全交给非沙盒守护
//       `pdf_daemon.py`（LaunchAgent com.wb.pdfquick，kqueue 盯队列目录）。
//
// ★★★ 为什么必须这样分（2026-10-02 实测，三组对照）：
//   A 组（抄 MouseBoost 的 entitlements：app-sandbox + user-selected）：
//       write_tmp=0  write_desktop=0  write_appsupport=0
//       NSTask task_launched=1 但 task_status=1（子进程同样被沙箱关住，干不了活）
//       NSWorkspace 拉 app = 1（这条通）
//   B 组（再加 temporary-exception 例外）：
//       write_tmp=0（/tmp/ 例外**不生效**）
//       write_appsupport=1  ← ★ 例外目录**能直写**
//       子进程仍 task_status=1
//   ⇒ 结论：**子进程这条线在沙箱里是死的，别指望**；
//            要干活只有两条路 ——（1）写「被例外放行的目录」，
//                                   （2）NSWorkspace 拉起非沙盒 app。
//     本文件走（1）：写进既有队列 ~/Library/Logs/mouseboost_pdf/queue/
//     ⇒ 守护 `pdf_daemon.py` 一个字都不用改就收得到（NEW_* 三个新动作除外）。
//
// 任务文件格式（与 `configure_mouseboost.py` 里 MouseBoost 那个右键脚本一字不差）：
//     第 1 行 = 动作（IMG2PDF / MERGE / CRYPT / DOC2PDF / NEW_XLSX / NEW_DOCX / NEW_PPTX）
//     第 2 行 = 通知标题
//     第 3 行 = 路径，每个用单引号包住、空格分隔（含空格的路径靠这个才不会被拆散）
// ============================================================================
#import <Cocoa/Cocoa.h>
#import <FinderSync/FinderSync.h>

// 与 configure_mouseboost.py / pdf_daemon.py 保持同一套扩展名清单
static NSString *const kImgExts =
    @"png,jpg,jpeg,heic,tiff,gif,bmp,webp";
static NSString *const kDocExts =
    @"doc,docx,docm,rtf,txt,odt,xls,xlsx,xlsm,csv,ods,ppt,pptx,pps,ppsx,odp";

static NSSet *SetOf(NSString *csv) {
    return [NSSet setWithArray:[csv componentsSeparatedByString:@","]];
}

// 前向声明（下面才定义；C 里用过再定义要报 implicit declaration）
static void WBScheduleSelfTest(void);
static NSArray<NSString *> *WBPick(NSArray<NSURL *> *urls, NSSet *exts);
static void WBDumpOrder(NSArray<NSURL *> *sel, NSArray<NSString *> *pdfs);

// 队列目录：写绝对路径（沙箱里 NSHomeDirectory() 指到容器里去，绝不能用它）
static NSString *WBQueueDir(void) {
    return [NSString stringWithFormat:@"/Users/%@/Library/Logs/mouseboost_pdf/queue",
            NSUserName()];
}

static BOOL WBWriteJob(NSString *action, NSString *title,
                       NSArray<NSString *> *paths) {
    NSString *dir = WBQueueDir();
    [[NSFileManager defaultManager] createDirectoryAtPath:dir
                              withIntermediateDirectories:YES
                                               attributes:nil error:NULL];
    NSMutableString *body = [NSMutableString stringWithFormat:@"%@\n%@\n",
                             action, title];
    if (paths.count) {
        NSMutableArray *q = [NSMutableArray arrayWithCapacity:paths.count];
        for (NSString *p in paths) {
            // 单引号是分隔符 ⇒ 路径里若本来有单引号要按 shell 规矩转义
            NSString *esc = [p stringByReplacingOccurrencesOfString:@"'"
                                                         withString:@"'\\''"];
            [q addObject:[NSString stringWithFormat:@"'%@'", esc]];
        }
        [body appendFormat:@"%@\n", [q componentsJoinedByString:@" "]];
    }
    NSString *job = [[[NSUUID UUID] UUIDString] stringByAppendingPathExtension:@"job"];
    job = [dir stringByAppendingPathComponent:job];
    NSError *e = nil;
    // ★ 就地写（atomically:NO）：原子写要"临时文件 + 改名"，改名的路在本项目沙箱里踩过坑。
    //   守护侧 `_job_ready()` 已经能应付"文件刚建好还没写完"的竞态（0 字节就等下一轮）。
    BOOL ok = [body writeToFile:job atomically:NO
                       encoding:NSUTF8StringEncoding error:&e];
    NSLog(@"WBRC: job_written=%d err=%ld", ok ? 1 : 0, (long)e.code);
    return ok;
}

// ── 动作 tag：写死的常量 ─────────────────────────────────────────────────
// ★★★ 为什么用 tag 而不是 representedObject（2026-10-02 用户报障「点了没反应」）：
//   菜单在扩展进程构建，但要**跨 XPC 交给访达显示**，点击再从访达回传。
//   这个边界只保留菜单项的基本属性 —— `representedObject` 过不去（到达时是 nil，
//   旧代码 `if (!d) return;` 正好在这里静默吞掉，日志都不打一条）。
//   `tag` 是 NSInteger，一定过得去；动作名直接写进 tag，无状态、可反复重建菜单。
//   ⚠️ 但 `target` 反过来**必须设**（见 item: 方法里的注释）—— 两者别搞混。
typedef NS_ENUM(NSInteger, WBActionTag) {
    WBActImg2PDF = 101,
    WBActMerge   = 102,
    WBActCrypt   = 103,
    WBActDoc2PDF = 104,
    WBActNewXLSX = 111,
    WBActNewDOCX = 112,
    WBActNewPPTX = 113,
};

// 构建菜单时把 payload 存在**本进程内存**里 —— `menuForMenuKind:` 与 `runAction:`
// 跑在同一个扩展进程，内存传递完全可靠、不经 XPC。（点击时若缓存空了，
// 再退一步现问 FIFinderSyncController 要选中项，双保险。）
static NSMutableDictionary<NSNumber *, id> *WBStore(void) {
    static NSMutableDictionary *d;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ d = [NSMutableDictionary dictionary]; });
    return d;
}

@interface WBRCFinderSync : FIFinderSync
@end

@implementation WBRCFinderSync

- (instancetype)init {
    self = [super init];
    if (self) {
        NSLog(@"WBRC: init");
        FIFinderSyncController *c = [FIFinderSyncController defaultController];
        // 整个盘都挂：右键菜单要在任何目录都能出现
        c.directoryURLs = [NSSet setWithObject:[NSURL fileURLWithPath:@"/"]];
        WBScheduleSelfTest();
    }
    return self;
}

// ── 链路自检（不必真的点菜单也能验证「扩展 → 队列 → 守护」通不通）
//    触发条件：存在 `~/Library/Logs/mouseboost_pdf/_selftest` 这个标记文件。
//    沙箱读不到 /tmp，但**读得到被例外放行的目录** ⇒ 标记文件放这里。
static void WBScheduleSelfTest(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)),
                       dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            NSString *flag = [NSString stringWithFormat:
                @"/Users/%@/Library/Logs/mouseboost_pdf/_selftest", NSUserName()];
            if (![[NSFileManager defaultManager] fileExistsAtPath:flag]) return;
            NSLog(@"WBRC: selftest_begin");
            WBWriteJob(@"PING", @"右键链路自检", @[]);
            NSLog(@"WBRC: selftest_end");
        });
    });
}

// ── 菜单 ─────────────────────────────────────────────────────────────────
- (NSMenu *)menuForMenuKind:(FIMenuKind)kind {
    NSLog(@"WBRC: menu_kind=%lu", (unsigned long)kind);
    NSMenu *m = [[NSMenu alloc] initWithTitle:@""];
    FIFinderSyncController *c = [FIFinderSyncController defaultController];

    if (kind == FIMenuKindContextualMenuForContainer) {
        // ① 空白处右键 → 新建文件（Windows 资源管理器那套习惯）
        NSURL *dir = c.targetedURL;
        if (dir) {
            [m addItem:[self item:@"新建 Excel 表格" tag:WBActNewXLSX payload:dir.path]];
            [m addItem:[self item:@"新建 Word 文档" tag:WBActNewDOCX payload:dir.path]];
            [m addItem:[self item:@"新建 PPT 演示文稿" tag:WBActNewPPTX payload:dir.path]];
        }
        NSLog(@"WBRC: menu_kind=%lu container_items=%lu",
              (unsigned long)kind, (unsigned long)m.numberOfItems);
        return m;
    }
    if (kind == FIMenuKindToolbarItemMenu) {
        return m;   // 工具栏那颗按钮上不挂东西
    }

    // ② 选中文件后右键 → 四个 PDF 相关动作（只显示"真有得处理"的那几项）
    NSArray<NSURL *> *sel = c.selectedItemURLs ?: @[];
    if (!sel.count) return m;

    NSArray<NSString *> *imgs = WBPick(sel, SetOf(kImgExts));
    NSArray<NSString *> *pdfs = WBPick(sel, [NSSet setWithObject:@"pdf"]);
    NSArray<NSString *> *docs = WBPick(sel, SetOf(kDocExts));

    if (imgs.count) {
        [m addItem:[self item:@"图片转PDF" tag:WBActImg2PDF payload:imgs]];
    }
    if (pdfs.count >= 2) {
        [m addItem:[self item:@"合并PDF" tag:WBActMerge payload:pdfs]];
    }
    if (pdfs.count >= 1) {
        [m addItem:[self item:@"PDF加密解密" tag:WBActCrypt payload:pdfs]];
    }
    if (docs.count) {
        [m addItem:[self item:@"文档转PDF" tag:WBActDoc2PDF payload:docs]];
    }
    NSLog(@"WBRC: menu_kind=%lu items=%lu img=%lu pdf=%lu doc=%lu",
          (unsigned long)kind, (unsigned long)m.numberOfItems,
          (unsigned long)imgs.count, (unsigned long)pdfs.count,
          (unsigned long)docs.count);

    // ★★ 顺序埋点（2026-10-02 用户报「合并顺序反了」）：
    //   `selectedItemURLs` 的**顺序**是未文档化的内部顺序。用户看到的是访达里的
    //   显示顺序，我们丢进队列的是这个数组的顺序 —— 两者若不一致，就会「反着合并」。
    //   ⚠️ 不能靠 NSLog 打文件名：日志里动态字符串默认被系统**隐私遮盖成 `<private>`**
    //      （实测整条 sel_order 都是 <private>，看不出任何顺序）⇒ 改写独立文件，
    //      落点就在被沙箱例外放行的目录里，直接 cat 即可。
    WBDumpOrder(sel, pdfs);
    return m;
}

static NSArray<NSString *> *WBPick(NSArray<NSURL *> *urls, NSSet *exts) {
    NSMutableArray *out = [NSMutableArray array];
    for (NSURL *u in urls) {
        NSString *e = [u.pathExtension lowercaseString];
        if (e.length && [exts containsObject:e]) [out addObject:u.path];
    }
    // ★★★ 绝不沿用 Finder 给的顺序（2026-10-02 用户报「合并顺序反了」的根因）：
    //   `selectedItemURLs` 的顺序**不是**「用户在访达里看到的上下顺序」，而是 Finder
    //   内部维护的一套顺序。同一批文件同一屏显示，实测三种操作拿到三种结果：
    //     鼠标点选后 ⌘A  → [X73, X70, X74, X75, X77, X78]
    //     键盘移动后 ⌘A  → [X70, X73, X74, X75, X77, X78]
    //     用户实际遇到    → [X78, X77, X75, X74, X73, X70]（完全逆序）
    //   ⇒ 「按选择顺序」这个语义在 FinderSync 里**根本无法实现**
    //      （Finder 从不提供「点击先后」，只给这个内部顺序）。
    //   改法：统一重排为**按名称的自然序** —— 即访达「按名称排序」时用户眼里的上下顺序
    //      （`localizedStandardCompare` 与访达的排序规则一致，X70 < X73 < … < X78）。
    return [out sortedArrayUsingComparator:^NSComparisonResult(NSString *a, NSString *b) {
        return [[a lastPathComponent] localizedStandardCompare:[b lastPathComponent]];
    }];
}

// 顺序埋点落盘：`~/Library/Logs/mouseboost_pdf/_order.txt`
// 第 1 行 `sel:`，随后每行一个「选中项」的文件名（Finder 给的顺序）；
// 再一行 `pdfs:`，随后每行一个我们**真正丢进队列**的 pdf 文件名。
// 用途：把「访达显示顺序 vs Finder 给我们的顺序」摆在一起，一眼看出有没有反。
static void WBDumpOrder(NSArray<NSURL *> *sel, NSArray<NSString *> *pdfs) {
    NSMutableArray<NSString *> *lines = [NSMutableArray array];
    [lines addObject:@"sel:"];
    for (NSURL *u in sel) [lines addObject:u.lastPathComponent];
    [lines addObject:@"pdfs:"];
    for (NSString *p in pdfs) [lines addObject:[p lastPathComponent]];
    NSString *body = [[lines componentsJoinedByString:@"\n"]
                      stringByAppendingString:@"\n"];
    NSString *path = [NSString stringWithFormat:
        @"/Users/%@/Library/Logs/mouseboost_pdf/_order.txt", NSUserName()];
    [body writeToFile:path atomically:NO
             encoding:NSUTF8StringEncoding error:NULL];
}

- (NSMenuItem *)item:(NSString *)title tag:(WBActionTag)tag payload:(id)payload {
    NSMenuItem *it = [[NSMenuItem alloc] initWithTitle:title
                                               action:@selector(runAction:)
                                        keyEquivalent:@""];
    // ★★★ target 必须设（Apple FinderSync 文档的要求）：
    //   系统靠 target 找到能响应 action 的对象；target 为 nil 时找不到响应者，
    //   该菜单项会被当成"没有动作"**静默过滤掉**（扩展侧构建了、访达侧不显示）。
    //   注：真正跨 XPC 过不去的是 `representedObject`（到达时是 nil）——
    //   所以动作名走 `tag`（NSInteger），payload 走本进程内存（WBStore）。
    it.target = self;
    it.tag = (NSInteger)tag;
    it.representedObject = nil;
    WBStore()[@(tag)] = payload ?: @[];
    return it;
}

// ── 执行：只丢任务文件，绝不起进程（沙箱里起子进程等于白起，实测 task_status=1）
- (void)runAction:(NSMenuItem *)sender {
    // ★ 第一行就打日志 —— 它落在「任何 early return 之前」。
    //   上一版把日志放在 `if (!d) return;` 之后，于是点击失效时连痕迹都没有，
    //   排查时只能靠猜。这条日志是「点击到底有没有回到扩展」的唯一判据。
    NSLog(@"WBRC: run_action tag=%ld title=%@", (long)sender.tag, sender.title);

    NSInteger tag = sender.tag;
    if (tag == 0) {
        // 兜底：万一 tag 也没过来，按标题反查（标题一定会保留，访达要靠它显示）
        NSString *t = sender.title ?: @"";
        if ([t isEqualToString:@"图片转PDF"])        tag = WBActImg2PDF;
        else if ([t isEqualToString:@"合并PDF"])     tag = WBActMerge;
        else if ([t isEqualToString:@"PDF加密解密"]) tag = WBActCrypt;
        else if ([t isEqualToString:@"文档转PDF"])   tag = WBActDoc2PDF;
        else if ([t containsString:@"Excel"])        tag = WBActNewXLSX;
        else if ([t containsString:@"Word"])         tag = WBActNewDOCX;
        else if ([t containsString:@"PPT"])          tag = WBActNewPPTX;
        NSLog(@"WBRC: run_action tag 缺失，按标题反查 -> %ld", (long)tag);
    }

    // 路径来源：① 菜单构建时存在本进程内存里的那份（最可靠）
    //           ② 缓存空就现问 FinderSync 要当前选中项（双保险）
    id cached = WBStore()[@(tag)];
    NSArray<NSString *> *paths = nil;
    if ([cached isKindOfClass:[NSArray class]] && [(NSArray *)cached count]) {
        paths = cached;
    } else if ([cached isKindOfClass:[NSString class]] && [(NSString *)cached length]) {
        paths = @[(NSString *)cached];
    }

    FIFinderSyncController *c = [FIFinderSyncController defaultController];
    NSArray<NSURL *> *sel = c.selectedItemURLs ?: @[];
    NSURL *dir = c.targetedURL;
    BOOL fromCache = (paths.count > 0);
    if (!paths.count) {
        if ([cached isKindOfClass:[NSString class]]) {
            paths = @[(NSString *)cached];
        } else if (tag == WBActNewXLSX || tag == WBActNewDOCX || tag == WBActNewPPTX) {
            paths = dir ? @[dir.path] : @[];
        } else if (tag == WBActImg2PDF) {
            paths = WBPick(sel, SetOf(kImgExts));
        } else if (tag == WBActDoc2PDF) {
            paths = WBPick(sel, SetOf(kDocExts));
        } else {
            paths = WBPick(sel, [NSSet setWithObject:@"pdf"]);
        }
    }

    NSString *action = nil, *title = nil;
    switch ((WBActionTag)tag) {
        case WBActImg2PDF: action = @"IMG2PDF";  title = @"图片转PDF";        break;
        case WBActMerge:   action = @"MERGE";    title = @"合并PDF";          break;
        case WBActCrypt:   action = @"CRYPT";    title = @"PDF加密解密";      break;
        case WBActDoc2PDF: action = @"DOC2PDF";  title = @"文档转PDF";        break;
        case WBActNewXLSX: action = @"NEW_XLSX"; title = @"新建 Excel 表格";  break;
        case WBActNewDOCX: action = @"NEW_DOCX"; title = @"新建 Word 文档";   break;
        case WBActNewPPTX: action = @"NEW_PPTX"; title = @"新建 PPT 演示文稿"; break;
        default:
            NSLog(@"WBRC: run_action 认不出这一项（tag=%ld title=%@）", (long)tag, sender.title);
            return;
    }

    if (!paths.count) {
        NSLog(@"WBRC: run_action 没拿到路径（action=%@ sel=%lu）",
              action, (unsigned long)sel.count);
        return;
    }

    NSLog(@"WBRC: run_action_ok %@ paths=%lu src=%@",
          action, (unsigned long)paths.count, fromCache ? @"cache" : @"live");
    WBWriteJob(action, title, paths);
}

@end
