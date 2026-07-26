# 框架指纹与专项验证路由

## 使用原则

专项技能必须建立在框架、版本、入口和前置条件证据之上。本文只描述路由与验证门禁，不提供可直接
复制的攻击载荷。

## ThinkPHP

候选信号包括 ThinkPHP 特征页面、路由格式、错误栈、静态资源或指纹工具一致命中。先使用
`nuclei` 的 ThinkPHP safe-poc 模板区分版本和漏洞族。只有证据指向受影响的 5.x 路由处理问题时，
才考虑 `exploit-thinkphp`，并使用无副作用的最小验证动作。验证失败后不要循环调用相同技能；应
回到版本、入口和路由模式确认。

## Apache Shiro

典型候选信号是认证流程和 `rememberMe` Cookie 行为，但单独看到 Cookie 名不足以证明存在默认
密钥反序列化风险。先确认 Shiro 使用情况和版本，再通过受控 safe-poc 验证密钥与反序列化条件。
`shiro_exploit` 只能在候选已确认、目标已授权且回连/验证环境受控时使用。

## Apache Tomcat

先通过 HTTP Header、错误页、管理路径或版本文件确认 Tomcat。CVE-2017-12615 还依赖特定版本、
DefaultServlet 写权限和配置条件，不能仅凭 Tomcat 7.x 就确认。专项验证应优先检查 HTTP 方法和
写入边界，不在目标上长期保留 JSP 或其他测试文件。

## Oracle WebLogic

通过 7001/7002 服务、控制台路径、T3/IIOP 服务特征和版本证据确认。不同 CVE 的入口和前置条件
不同，必须先选择匹配的 safe-poc。`exploit-weblogic` 只在漏洞族明确后使用，避免对不相关端口
发送反序列化数据。

## Fastjson

候选信号来自 JSON API、异常信息、依赖版本或明确的 Fastjson 解析行为。不能因为 Spring Boot
或 Java REST API 就推断使用 Fastjson。只有确认受影响版本、autoType/反序列化入口和受控回连
条件时，才进入 `fastjson-exploit` 或 `jndi_exploit` 路径。

## Struts2

通过 action 路径、错误栈、OGNL 特征和版本证据识别。Struts2 漏洞数量多且适用条件差异大，
应使用 `nuclei` 的 `struts2` 路由选择具体模板，不使用通用命令执行猜测。命中后记录 S2 编号、
入口、参数位置和响应证据。

## Spring 生态

区分 Spring Framework、Spring Cloud Gateway、Spring Cloud Function、Spring Data 等组件。
“使用 Spring”不是 Spring4Shell 或 SpEL 漏洞证据。应依据组件版本、部署方式、路由或函数入口
选择 `spring` 模板，并在报告中写清适用前置条件。

## Jenkins、Nacos、Solr 与 Elasticsearch

- Jenkins：确认管理入口、版本、匿名访问边界和具体功能端点；
- Nacos：区分未授权访问、认证绕过和数据库查询问题，不把登录页暴露等同于漏洞；
- Solr：确认 Core、Config、DataImport 等具体入口，再选择对应模板；
- Elasticsearch：确认版本、脚本功能和快照/路径配置，避免依据 9200 端口直接套用旧漏洞。

## 专项路由退出条件

满足以下任一条件时停止该专项路径：

- 产品或版本证据排除受影响范围；
- 必要入口不可达或配置前置条件不存在；
- safe-poc 已稳定确认并已进入报告/最小影响验证；
- 连续执行没有产生新证据；
- 继续验证会超出授权范围或带来不必要影响。
