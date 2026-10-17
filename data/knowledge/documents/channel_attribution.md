# 渠道归属与字段说明

Synthetic business knowledge — 合成业务知识，不来源于真实企业。

每张订单通过 orders.channel_id 关联 channels.channel_id，只记录一个渠道。
当前数据没有曝光、点击或多触点链路；不能推断首触、末触、跨设备归因或真实广告平台规则。

| channel_id | channel_name | channel_type | is_paid |
| --- | --- | --- | --- |
| direct | 直接访问 | owned | false |
| search | 搜索 | search | true |
| social | 社交 | social | true |
| affiliate | 联盟 | partner | true |

channel_name 是展示名称，channel_type 是渠道类别，is_paid 表示合成配置中是否为付费渠道。
direct 是自有直接访问；search 是搜索渠道；social 是社交来源；affiliate 是合作联盟来源。
付费标记不等于已提供广告花费。数据没有预算、点击单价或佣金表，不能计算真实投放回报或获客成本。

比较渠道表现需要先选定指标。成交规模、净额和退款比例回答不同问题，不能仅凭渠道类型推断表现优劣。
渠道归属采用订单已有外键，不按商品类别或退款原因重新分配渠道。
