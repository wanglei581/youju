// 这几个测试文件里的用例测的是 AI 内测包（config.aiInternal 为 true）的行为："内部测试"标注、
// 对白名单外的人只写"暂时没有可用工具"、协议草案也开放同意等。10/18 起主分支就是提审用的完整正式包
// （aiInternal 为 false），这里在加载其他模块之前把配置换成内测包，不随真实配置变化；
// 正式包的行为另有用例（ai-entry.test.js 里注入 aiInternal: false 的那些）。
// node --test 每个测试文件单独一个进程，这里改的模块缓存只影响引用它的文件。
const configPath = require.resolve('../../utils/config.js');
const real = require(configPath);
require.cache[configPath].exports = Object.freeze({ ...real, aiInternal: true });
module.exports = require.cache[configPath].exports;
