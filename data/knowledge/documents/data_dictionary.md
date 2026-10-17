# 合成电商数据字典

Synthetic business knowledge — 合成业务知识，不来源于真实企业。

业务数据由 channels、products、orders、order_items、refunds 五张表组成。
以下描述字段的含义和关联边界，不包含评测答案或隐藏场景。
所有金额以 CNY 表达，数据库金额使用 DECIMAL(12,2)。业务时间为 Asia/Shanghai 本地 TIMESTAMP。
时间字段记录不同事件，不能因为字段类型相同就相互替换。

## channels：渠道维表

channels.channel_id 是渠道主键，供订单引用；channels.channel_name 是中文展示名称。
channels.channel_type 是来源类别，channels.is_paid 是是否属于合成付费渠道的布尔标记。
一个渠道可以关联多张订单。渠道维表没有广告预算、曝光数或点击数。
渠道的付费标记只说明类别，不能用于计算成本、投放效率或回报。

## products：商品维表

products.product_id 是商品主键；products.sku 是唯一库存编码；products.product_name 是合成商品名称。
products.category 表示商品品类，products.brand 表示合成品牌，products.is_active 表示是否启用。
products.unit_cost 是商品单位成本，products.list_price 是商品标价，二者都是非负金额。
标价不是每一笔交易的实际支付金额；交易行保存自己的单价及优惠。
本模型没有库存流水、运费、税费或成本变化历史，不能把简化商品成本解释为完整利润核算。

## orders：订单事实表

orders.order_id 是订单主键；orders.customer_id 是合成客户标识，当前没有独立客户画像表。
orders.channel_id 是渠道外键；orders.region 是订单地区；orders.currency 固定为 CNY。
orders.order_status 记录 paid、completed 或 cancelled；状态约束与时间字段一起定义交易事件。
orders.created_at 是创建时间；orders.paid_at 是付款时间；orders.cancelled_at 是取消时间。
成交状态必须有付款时间且无取消时间，取消状态必须有取消时间且无付款时间。
客户标识不包含真实个人资料，也不足以支持年龄、性别或终身价值等未提供的属性分析。

## order_items：商品行事实表

order_items.order_item_id 是商品行主键；order_items.order_id 指向所属订单。
order_items.product_id 指向商品维表；order_items.quantity 是正整数购买数量。
order_items.unit_price 是交易单价；order_items.discount_amount 是整条商品行的优惠金额。
商品行金额为 quantity 乘以 unit_price 后减去 discount_amount，不应再按数量乘一次优惠。
同一订单可以有多条商品行，所以行数和订单数并不相等。
连接商品与订单时要保留各自粒度；订单级去重数量不能通过商品分组结果任意相加。

## refunds：退款申请事实表

refunds.refund_id 是退款申请主键；refunds.order_id 指向原订单。
refunds.order_item_id 指向关联商品行，schema 允许为空，但当前生成数据使用商品行级退款。
refunds.refund_amount 是申请退款金额；refunds.refund_reason 是申请原因。
refunds.refund_status 是 approved、rejected 或 pending；refunds.created_at 是退款申请时间。
当前模型没有审批时间或到账时间。退款申请时间不等于订单付款时间，也不等于实际到账时间。
退款商品行必须属于退款记录所引用的订单，金额不能超出关联商品行金额。
连接多条商品行和多条退款记录可能扩大行数；应先明确度量粒度，不能直接汇总重复行。

## 关系与口径约束

channels 与 orders 为一对多；orders 与 order_items 为一对多；products 与 order_items 为一对多。
orders 与 refunds 为一对多；退款的商品行关系是可选外键，不能把可空设计误读为已实现订单级退款分摊。
正式指标可用维度由 Metric Contract 决定，字段存在不等于业务上允许任意分组。
上述字典解释存储含义，指标的版本、时间归属、过滤条件与依赖由注册表自动渲染。
