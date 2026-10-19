// main.m —— FinderSync 扩展入口
//
// ★★★ 必须是 NSExtensionMain（不是 NSApplicationMain）：
//   实测对照 2026-10-02 —— MouseBoost/WeType/Doubao 三个能正常工作的扩展
//   都引用了 `_NSExtensionMain`；只用 NSApplicationMain 时进程能起来、
//   但 PluginKit 永远不会实例化 principal class（init() 不被调用，菜单永不出现）。
#import <Cocoa/Cocoa.h>

extern int NSExtensionMain(int argc, char *argv[]);

int main(int argc, const char *argv[]) {
    NSLog(@"WBQA: main() entered (NSExtensionMain)");
    return NSExtensionMain(argc, (char **)argv);
}
