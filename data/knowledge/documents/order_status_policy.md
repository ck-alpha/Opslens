# 订单状态与成交政策

Synthetic business knowledge — 合成业务知识，不来源于真实企业。

orders.order_status 的 paid 表示已付款，completed 表示已完成，cancelled 表示取消。
paid 和 completed 属于成交订单集合，必须有 paid_at；是否退款不会自动移除成交订单。
cancelled 没有 paid_at，必须有 cancelled_at，因此不能作为已成交订单计入正式成交指标。
这是当前 synthetic 数据状态模型，不意味着任何真实企业的已付款订单都不能取消。

orders.created_at 是订单创建时间，可用于描述下单事件；orders.paid_at 是付款时间，用于现行成交指标。
创建订单与付款成交不是同一个事件。跨时间范围分析应声明用的是哪一个事件的日期。
orders.cancelled_at 记录取消时间，不应该用来填补缺失的付款时间。

一张订单可能有多条商品行。order_items 的行数不是成交订单数，连接商品行后不能直接把行数当订单数。
取消订单也可以具有商品行，但存在商品行不能证明订单已经成交。
金额的行级优惠 discount_amount 是整条商品行优惠，不是每件商品优惠。
