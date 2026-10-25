name := "good-traffic-load-tester"
version := "1.0"
scalaVersion := "2.13.12"

enablePlugins(GatlingPlugin)

// Check for newer Gatling releases before the event - pin to whatever
// `sbt gatling:test` actually resolves cleanly in your environment.
libraryDependencies ++= Seq(
  "io.gatling.highcharts" % "gatling-charts-highcharts" % "3.13.5" % "test,it",
  "io.gatling" % "gatling-test-framework" % "3.13.5" % "test,it"
)
